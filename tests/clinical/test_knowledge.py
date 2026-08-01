"""Tests for the clinical knowledge-source clients (openFDA / RxNorm /
PubMed) and the deterministic drug-interaction engine.

All HTTP traffic is mocked via ``httpx.MockTransport`` — no real network
access. A single ``@pytest.mark.integration`` test hits the live openFDA API
and is skipped by default (see ``pyproject.toml`` ``addopts``).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from doctoragent.clinical.knowledge import (
    OpenFDAClient,
    PubMedClient,
    RxNormClient,
    check_drug_interactions,
    get_severity_rank,
)
from doctoragent.clinical.knowledge.drug_interactions import DrugInteractionResult

# ---------------------------------------------------------------------------
# MockTransport handler factories
# ---------------------------------------------------------------------------


def _openfda_handler(
    label_results: list[dict] | None = None,
    event_results: list[dict] | None = None,
    status: int = 200,
):
    """Build a MockTransport handler emulating the openFDA endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if status == 404:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        if "/drug/label.json" in url:
            return httpx.Response(200, json={"results": label_results or []})
        if "/drug/event.json" in url:
            return httpx.Response(200, json={"results": event_results or []})
        return httpx.Response(404)

    return handler


def _rxnorm_handler(
    approximate: dict | None = None,
    properties: dict | None = None,
    brands: dict | None = None,
    allrelated: dict | None = None,
    rxclass: dict | None = None,
    status: int = 200,
):
    """Build a MockTransport handler emulating the RxNorm endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if status == 404:
            return httpx.Response(404)
        if "approximateTerm" in url:
            return httpx.Response(200, json=approximate or {})
        if "properties" in url:
            return httpx.Response(200, json=properties or {})
        if "brands" in url:
            return httpx.Response(200, json=brands or {})
        if "allrelated" in url:
            return httpx.Response(200, json=allrelated or {})
        if "rxclass" in url:
            return httpx.Response(200, json=rxclass or {})
        return httpx.Response(404)

    return handler


def _pubmed_handler(
    esearch: dict | None = None,
    esummary: dict | None = None,
    efetch_xml: str | None = None,
    status: int = 200,
):
    """Build a MockTransport handler emulating the NCBI E-utilities endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if status == 404:
            return httpx.Response(404)
        if "esearch.fcgi" in url:
            return httpx.Response(200, json=esearch or {})
        if "esummary.fcgi" in url:
            return httpx.Response(200, json=esummary or {})
        if "efetch.fcgi" in url:
            return httpx.Response(
                200, text=efetch_xml or "", headers={"Content-Type": "text/xml"}
            )
        return httpx.Response(404)

    return handler


# ---------------------------------------------------------------------------
# OpenFDA tests
# ---------------------------------------------------------------------------


async def test_openfda_search_drug_label_returns_results() -> None:
    handler = _openfda_handler(
        label_results=[{"id": "abc", "openfda": {"generic_name": ["ibuprofen"]}}]
    )
    client = OpenFDAClient(transport=httpx.MockTransport(handler))
    try:
        results = await client.search_drug_label("ibuprofen")
        assert len(results) == 1
        assert results[0]["openfda"]["generic_name"] == ["ibuprofen"]
    finally:
        await client.close()


async def test_openfda_search_drug_label_404_returns_empty() -> None:
    handler = _openfda_handler(status=404)
    client = OpenFDAClient(transport=httpx.MockTransport(handler))
    try:
        results = await client.search_drug_label("nonexistent-drug")
        assert results == []
    finally:
        await client.close()


async def test_openfda_get_drug_label_by_spl_set_id() -> None:
    handler = _openfda_handler(
        label_results=[{"id": "abc", "openfda": {"spl_set_id": ["xyz123"]}}]
    )
    client = OpenFDAClient(transport=httpx.MockTransport(handler))
    try:
        label = await client.get_drug_label("xyz123")
        assert label["openfda"]["spl_set_id"] == ["xyz123"]
    finally:
        await client.close()


async def test_openfda_get_drug_label_empty_when_no_match() -> None:
    handler = _openfda_handler(label_results=[])
    client = OpenFDAClient(transport=httpx.MockTransport(handler))
    try:
        label = await client.get_drug_label("missing")
        assert label == {}
    finally:
        await client.close()


async def test_openfda_search_adverse_events() -> None:
    handler = _openfda_handler(
        event_results=[{"safetyreportid": "1"}, {"safetyreportid": "2"}]
    )
    client = OpenFDAClient(transport=httpx.MockTransport(handler))
    try:
        events = await client.search_adverse_events("ibuprofen")
        assert len(events) == 2
        assert events[0]["safetyreportid"] == "1"
    finally:
        await client.close()


async def test_openfda_get_interactions_section_extracts_text() -> None:
    handler = _openfda_handler(
        label_results=[{"drug_interactions": ["Contraindicated with warfarin."]}]
    )
    client = OpenFDAClient(transport=httpx.MockTransport(handler))
    try:
        text = await client.get_interactions_section("ibuprofen")
        assert "warfarin" in text.lower()
        assert "contraindicated" in text.lower()
    finally:
        await client.close()


async def test_openfda_get_interactions_section_empty_when_no_field() -> None:
    handler = _openfda_handler(label_results=[{"id": "abc"}])
    client = OpenFDAClient(transport=httpx.MockTransport(handler))
    try:
        text = await client.get_interactions_section("ibuprofen")
        assert text == ""
    finally:
        await client.close()


async def test_openfda_rate_limit_reflects_api_key() -> None:
    no_key = OpenFDAClient()
    with_key = OpenFDAClient(api_key="secret")
    try:
        assert no_key.rate_limit_per_minute == 40
        assert with_key.rate_limit_per_minute == 240
    finally:
        await no_key.close()
        await with_key.close()


# ---------------------------------------------------------------------------
# RxNorm tests
# ---------------------------------------------------------------------------


async def test_rxnorm_normalize_drug_name_returns_rxcui() -> None:
    handler = _rxnorm_handler(
        approximate={
            "approximateGroup": {
                "candidate": [{"rxcui": "161", "name": "acetaminophen"}]
            }
        }
    )
    client = RxNormClient(transport=httpx.MockTransport(handler))
    try:
        rxcui = await client.normalize_drug_name("acetaminophen")
        assert rxcui == "161"
    finally:
        await client.close()


async def test_rxnorm_normalize_drug_name_returns_none_on_empty_candidates() -> None:
    handler = _rxnorm_handler(
        approximate={"approximateGroup": {"candidate": []}}
    )
    client = RxNormClient(transport=httpx.MockTransport(handler))
    try:
        rxcui = await client.normalize_drug_name("nonsense")
        assert rxcui is None
    finally:
        await client.close()


async def test_rxnorm_normalize_drug_name_returns_none_on_404() -> None:
    handler = _rxnorm_handler(status=404)
    client = RxNormClient(transport=httpx.MockTransport(handler))
    try:
        rxcui = await client.normalize_drug_name("nonsense")
        assert rxcui is None
    finally:
        await client.close()


async def test_rxnorm_get_drug_info_aggregates_fields() -> None:
    handler = _rxnorm_handler(
        properties={"properties": {"name": "Acetaminophen", "tty": "IN"}},
        brands={"brandGroup": {"brand": [{"name": "Tylenol"}]}},
        allrelated={
            "allRelatedGroup": {
                "conceptGroup": [
                    {
                        "tty": "SCD",
                        "conceptProperties": [
                            {"rxcui": "1", "name": "Acetaminophen 325 MG"}
                        ],
                    }
                ]
            }
        },
    )
    client = RxNormClient(transport=httpx.MockTransport(handler))
    try:
        info = await client.get_drug_info("161")
        assert info["rxcui"] == "161"
        assert info["name"] == "Acetaminophen"
        assert info["tty"] == "IN"
        assert "Tylenol" in info["brand_names"]
        assert any("325 MG" in n for n in info["ingredient_of"])
    finally:
        await client.close()


async def test_rxnorm_get_related_drugs_filters_by_relation() -> None:
    handler = _rxnorm_handler(
        allrelated={
            "allRelatedGroup": {
                "conceptGroup": [
                    {
                        "tty": "IN",
                        "conceptProperties": [
                            {"rxcui": "161", "name": "Acetaminophen"}
                        ],
                    },
                    {
                        "tty": "SCD",
                        "conceptProperties": [{"rxcui": "1", "name": "Other"}],
                    },
                ]
            }
        }
    )
    client = RxNormClient(transport=httpx.MockTransport(handler))
    try:
        related = await client.get_related_drugs("161", relation="IN")
        assert len(related) == 1
        assert related[0]["name"] == "Acetaminophen"
        assert related[0]["tty"] == "IN"
    finally:
        await client.close()


async def test_rxnorm_get_drug_classes_dedupes() -> None:
    handler = _rxnorm_handler(
        rxclass={
            "rxclassDrugInfoList": {
                "rxclassDrugInfo": [
                    {
                        "rxclassMinConceptItem": {
                            "classId": "N02BE01",
                            "className": "Acetaminophen",
                            "classType": "ATC",
                        }
                    },
                    {
                        "rxclassMinConceptItem": {
                            "classId": "N02BE01",
                            "className": "Acetaminophen",
                            "classType": "ATC",
                        }
                    },
                ]
            }
        }
    )
    client = RxNormClient(transport=httpx.MockTransport(handler))
    try:
        classes = await client.get_drug_classes("161")
        assert len(classes) == 1
        assert classes[0]["class_id"] == "N02BE01"
        assert classes[0]["type"] == "ATC"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# check_drug_interactions tests (pure logic with injected fake clients)
# ---------------------------------------------------------------------------


class _FakeRxNorm:
    """In-memory RxNorm stand-in for deterministic DDI tests."""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self.mapping = mapping
        self.closed = False

    async def normalize_drug_name(self, name: str) -> str | None:
        return self.mapping.get(name)

    async def close(self) -> None:
        self.closed = True


class _FakeOpenFDA:
    """In-memory openFDA stand-in for deterministic DDI tests."""

    def __init__(self, labels: dict[str, str]) -> None:
        self.labels = labels
        self.closed = False

    async def get_interactions_section(self, drug_name: str) -> str:
        return self.labels.get(drug_name, "")

    async def close(self) -> None:
        self.closed = True


async def test_check_drug_interactions_detects_contraindicated_pair() -> None:
    # warfarin + aspirin 命中本地 DDI 知识库（contraindicated），本地结果优先返回。
    rxnorm = _FakeRxNorm({"warfarin": "11289", "aspirin": "1191"})
    openfda = _FakeOpenFDA(
        {
            "warfarin": "Contraindicated with aspirin due to bleeding risk.",
            "aspirin": "Use with caution.",
        }
    )
    results = await check_drug_interactions(
        ["warfarin", "aspirin"], rxnorm=rxnorm, openfda=openfda
    )
    assert len(results) == 1
    r = results[0]
    assert {r.drug_a, r.drug_b} == {"warfarin", "aspirin"}
    assert r.severity == "contraindicated"
    # 本地知识库命中优先于 openFDA，且带结构化 mechanism/management 字段。
    assert r.source == "local"
    assert r.management
    # Injected clients are owned by the test — engine must not close them.
    assert not rxnorm.closed
    assert not openfda.closed


async def test_check_drug_interactions_local_kb_overrides_openfda_label() -> None:
    # warfarin + ibuprofen：本地知识库标记为 major；即使 openFDA 标签文本含
    # "Contraindicated"，本地结构化条目也优先返回（带 mechanism/management）。
    rxnorm = _FakeRxNorm({"warfarin": "11289", "ibuprofen": "5640"})
    openfda = _FakeOpenFDA(
        {
            "warfarin": "Contraindicated with ibuprofen due to bleeding risk.",
            "ibuprofen": "Use with caution.",
        }
    )
    results = await check_drug_interactions(
        ["warfarin", "ibuprofen"], rxnorm=rxnorm, openfda=openfda
    )
    assert len(results) == 1
    r = results[0]
    assert {r.drug_a, r.drug_b} == {"warfarin", "ibuprofen"}
    assert r.severity == "major"
    assert r.source == "local"
    assert r.mechanism
    assert r.management
    # 本地覆盖该药物对 → openfda 未被调用，注入的客户端不得被关闭。
    assert not rxnorm.closed
    assert not openfda.closed


async def test_check_drug_interactions_no_interaction_returns_empty() -> None:
    rxnorm = _FakeRxNorm({"aspirin": "1191", "acetaminophen": "161"})
    openfda = _FakeOpenFDA(
        {
            "aspirin": "May cause GI bleeding.",
            "acetaminophen": "Hepatotoxicity in overdose.",
        }
    )
    results = await check_drug_interactions(
        ["aspirin", "acetaminophen"], rxnorm=rxnorm, openfda=openfda
    )
    assert results == []


async def test_check_drug_interactions_severity_sorting() -> None:
    rxnorm = _FakeRxNorm({"warfarin": "1", "aspirin": "2", "ibuprofen": "3"})
    openfda = _FakeOpenFDA(
        {
            "warfarin": "",
            "aspirin": "",
            "ibuprofen": "",
        }
    )
    results = await check_drug_interactions(
        ["warfarin", "aspirin", "ibuprofen"],
        rxnorm=rxnorm,
        openfda=openfda,
    )
    # warfarin<->aspirin: 本地 contraindicated；warfarin<->ibuprofen: 本地 major；
    # aspirin<->ibuprofen: 未命中本地且 openFDA 标签无互提 → 无结果。
    assert len(results) == 2
    severities = [r.severity for r in results]
    assert severities == sorted(
        severities, key=lambda s: -get_severity_rank(s)
    )
    assert severities[0] == "contraindicated"
    assert severities[1] == "major"


async def test_check_drug_interactions_single_drug_returns_empty() -> None:
    # No clients injected → would create real ones; the < 2 guard must short-
    # circuit before any HTTP client is built.
    results = await check_drug_interactions(["aspirin"])
    assert results == []


async def test_check_drug_interactions_empty_list_returns_empty() -> None:
    results = await check_drug_interactions([])
    assert results == []


async def test_check_drug_interactions_default_severity_is_moderate() -> None:
    rxnorm = _FakeRxNorm({"drug_a": "1", "drug_b": "2"})
    openfda = _FakeOpenFDA(
        {"drug_a": "Co-administration with drug_b may alter levels.", "drug_b": ""}
    )
    results = await check_drug_interactions(
        ["drug_a", "drug_b"], rxnorm=rxnorm, openfda=openfda
    )
    assert len(results) == 1
    # No explicit severity keyword → defaults to moderate per spec.
    assert results[0].severity == "moderate"


def test_get_severity_rank_ordering() -> None:
    assert get_severity_rank("contraindicated") == 4
    assert get_severity_rank("major") == 3
    assert get_severity_rank("moderate") == 2
    assert get_severity_rank("minor") == 1
    assert get_severity_rank("unknown") == 0
    # Unknown labels collapse to 0; comparison is case-insensitive.
    assert get_severity_rank("nonsense") == 0
    assert get_severity_rank("CONTRAINDICATED") == 4
    assert get_severity_rank("Major") == 3
    # Strict monotonicity of the safety ordering.
    assert (
        get_severity_rank("contraindicated")
        > get_severity_rank("major")
        > get_severity_rank("moderate")
        > get_severity_rank("minor")
        > get_severity_rank("unknown")
    )


def test_drug_interaction_result_defaults() -> None:
    r = DrugInteractionResult(drug_a="a", drug_b="b")
    assert r.severity == "moderate"
    assert r.source == "openfda"
    assert r.description == ""
    assert r.mechanism == ""
    assert r.clinical_effect == ""
    assert r.management == ""


def test_drug_interaction_result_full_construction() -> None:
    r = DrugInteractionResult(
        drug_a="a",
        drug_b="b",
        severity="major",
        description="desc",
        mechanism="mech",
        clinical_effect="effect",
        management="mgmt",
        source="rxnorm",
    )
    assert r.severity == "major"
    assert r.source == "rxnorm"
    assert r.mechanism == "mech"


# ---------------------------------------------------------------------------
# PubMed tests
# ---------------------------------------------------------------------------


async def test_pubmed_search_returns_summaries() -> None:
    handler = _pubmed_handler(
        esearch={"esearchresult": {"idlist": ["1", "2"]}},
        esummary={
            "result": {
                "uids": ["1", "2"],
                "1": {
                    "title": "Paper 1",
                    "authors": [{"name": "Doe J"}],
                    "source": "Nature",
                    "pubdate": "2024",
                },
                "2": {
                    "title": "Paper 2",
                    "authors": [],
                    "source": "Cell",
                    "pubdate": "2023",
                },
            }
        },
    )
    client = PubMedClient(transport=httpx.MockTransport(handler))
    try:
        results = await client.search("cancer immunotherapy")
        assert len(results) == 2
        assert results[0]["pmid"] == "1"
        assert results[0]["title"] == "Paper 1"
        assert results[0]["authors"] == ["Doe J"]
        assert results[0]["journal"] == "Nature"
        assert results[0]["pubdate"] == "2024"
        # esummary does not return abstracts — field present but empty.
        assert results[0]["abstract"] == ""
    finally:
        await client.close()


async def test_pubmed_search_empty_idlist_returns_empty() -> None:
    handler = _pubmed_handler(esearch={"esearchresult": {"idlist": []}})
    client = PubMedClient(transport=httpx.MockTransport(handler))
    try:
        results = await client.search("nonexistent topic")
        assert results == []
    finally:
        await client.close()


async def test_pubmed_get_abstract_parses_xml() -> None:
    xml = (
        "<PubmedArticleSet>"
        "<PubmedArticle>"
        "<MedlineCitation>"
        "<Article>"
        "<ArticleTitle>Title</ArticleTitle>"
        "<Abstract><AbstractText>This is the abstract.</AbstractText></Abstract>"
        "</Article>"
        "</MedlineCitation>"
        "</PubmedArticle>"
        "</PubmedArticleSet>"
    )
    handler = _pubmed_handler(efetch_xml=xml)
    client = PubMedClient(transport=httpx.MockTransport(handler))
    try:
        abstract = await client.get_abstract("12345")
        assert abstract is not None
        assert "This is the abstract" in abstract
    finally:
        await client.close()


async def test_pubmed_get_abstract_none_on_404() -> None:
    handler = _pubmed_handler(status=404)
    client = PubMedClient(transport=httpx.MockTransport(handler))
    try:
        abstract = await client.get_abstract("00000")
        assert abstract is None
    finally:
        await client.close()


async def test_pubmed_search_clinical_applies_filter() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "esearch.fcgi" in url:
            # Only the esearch request carries the `term` param; capture it
            # here so the later esummary call does not overwrite with "".
            captured["term"] = request.url.params.get("term", "")
            return httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}})
        if "esummary.fcgi" in url:
            return httpx.Response(
                200,
                json={
                    "result": {
                        "1": {
                            "title": "Trial",
                            "authors": [],
                            "source": "Lancet",
                            "pubdate": "2024",
                        }
                    }
                },
            )
        return httpx.Response(404)

    client = PubMedClient(transport=httpx.MockTransport(handler))
    try:
        results = await client.search_clinical("diabetes")
        assert len(results) == 1
        assert results[0]["title"] == "Trial"
        # The clinical-query filter must be appended to the user query.
        assert "diabetes" in captured["term"]
        assert "Clinical Trial" in captured["term"]
    finally:
        await client.close()


async def test_pubmed_rate_limit_reflects_api_key() -> None:
    no_key = PubMedClient()
    with_key = PubMedClient(api_key="secret")
    try:
        assert no_key.rate_limit_per_second == 3
        assert with_key.rate_limit_per_second == 10
    finally:
        await no_key.close()
        await with_key.close()


# ---------------------------------------------------------------------------
# Live integration test (skipped by default)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_openfda_live_smoke() -> None:
    """Live openFDA smoke test — run with ``-m integration``."""
    client = OpenFDAClient()
    try:
        results = await client.search_drug_label("ibuprofen", limit=1)
        assert isinstance(results, list)
    finally:
        await client.close()
