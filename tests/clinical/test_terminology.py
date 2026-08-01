"""Comprehensive tests for the terminology binding package.

Covers:
* :mod:`codesystems` — URI parsing + canonical URIs (incl. SNOMED CT
  edition suffixes, vendor variants, malformed input).
* :mod:`loinc_map` — curated lookups + bulk-table loader.
* :mod:`icd10_map` — curated lookups + structural validator + bulk-table loader.
* :mod:`snowstorm` — SnowstormClient happy path, 404, 5xx retry, malformed
  JSON, ancestors, is_a, async lifecycle.
* :mod:`service` (TerminologyService façade) — sync + async paths,
  fallback display, resolve_coding / resolve_codeable_concept, graceful
  degradation when no Snowstorm/RxNorm client configured, error paths.

All HTTP traffic is mocked via :class:`httpx.MockTransport`; no real
network access. The bulk-table loader tests use ``tmp_path`` fixtures.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from doctoragent.clinical.terminology import (
    CODE_SYSTEM_ICD10_CM,
    CODE_SYSTEM_LOINC,
    CODE_SYSTEM_RXNORM,
    CODE_SYSTEM_SNOMED_CT,
    ICD10_DISPLAYS,
    LOINC_DISPLAYS,
    CodeSystem,
    SnowstormClient,
    SnowstormError,
    SnowstormNotFoundError,
    TerminologyService,
    TerminologyServiceError,
    TerminologyServiceResult,
    lookup_display,
    lookup_icd10_display,
    lookup_loinc_display,
    lookup_loinc_test_name,
    parse_system_uri,
)


# --------------------------------------------------------------------------- #
# codesystems
# --------------------------------------------------------------------------- #
class TestParseSystemUri:
    """Tolerant parsing of FHIR ``Coding.system`` strings."""

    def test_loinc_canonical(self) -> None:
        assert parse_system_uri("http://loinc.org") == CodeSystem.LOINC

    def test_loinc_https_variant(self) -> None:
        assert parse_system_uri("https://loinc.org") == CodeSystem.LOINC

    def test_snomed_ct_canonical(self) -> None:
        assert parse_system_uri("http://snomed.info/sct") == CodeSystem.SNOMED_CT

    def test_snomed_ct_https_variant(self) -> None:
        assert parse_system_uri("https://snomed.info/sct") == CodeSystem.SNOMED_CT

    def test_snomed_ct_us_edition_suffix(self) -> None:
        # US edition: http://snomed.info/sct/731000124108
        assert parse_system_uri(
            "http://snomed.info/sct/731000124108"
        ) == CodeSystem.SNOMED_CT

    def test_snomed_ct_edition_with_version(self) -> None:
        assert parse_system_uri(
            "http://snomed.info/sct/731000124108/2024-09"
        ) == CodeSystem.SNOMED_CT

    def test_icd10_cm_canonical(self) -> None:
        assert parse_system_uri(
            "http://hl7.org/fhir/sid/icd-10-cm"
        ) == CodeSystem.ICD10_CM

    def test_icd10_cm_https_variant(self) -> None:
        assert parse_system_uri(
            "https://hl7.org/fhir/sid/icd-10-cm"
        ) == CodeSystem.ICD10_CM

    def test_icd10_cm_short_form(self) -> None:
        # Some vendor servers emit the bare code-system name.
        assert parse_system_uri("icd-10-cm") == CodeSystem.ICD10_CM

    def test_rxnorm_canonical(self) -> None:
        assert parse_system_uri(
            "http://www.nlm.nih.gov/research/umls/rxnorm"
        ) == CodeSystem.RXNORM

    def test_rxnorm_https_variant(self) -> None:
        assert parse_system_uri(
            "https://www.nlm.nih.gov/research/umls/rxnorm"
        ) == CodeSystem.RXNORM

    def test_rxnorm_oid_form(self) -> None:
        assert parse_system_uri("urn:oid:2.16.840.1.113883.6.88") == CodeSystem.RXNORM

    def test_unknown_uri(self) -> None:
        assert parse_system_uri("http://example.com/unknown") == CodeSystem.UNKNOWN

    def test_none_input(self) -> None:
        assert parse_system_uri(None) == CodeSystem.UNKNOWN

    def test_empty_string(self) -> None:
        assert parse_system_uri("") == CodeSystem.UNKNOWN

    def test_non_string_input(self) -> None:
        # Defensive: some buggy FHIR servers emit the system as a number.
        assert parse_system_uri(123) == CodeSystem.UNKNOWN  # type: ignore[arg-type]

    def test_whitespace_tolerant(self) -> None:
        assert parse_system_uri("  http://loinc.org  ") == CodeSystem.LOINC

    def test_case_insensitive(self) -> None:
        # Vendors sometimes capitalise the scheme.
        assert parse_system_uri("HTTP://LOINC.ORG") == CodeSystem.LOINC


class TestCanonicalUris:
    """CodeSystem enum exposes canonical_uri."""

    @pytest.mark.parametrize(
        "system, expected",
        [
            (CodeSystem.LOINC, CODE_SYSTEM_LOINC),
            (CodeSystem.SNOMED_CT, CODE_SYSTEM_SNOMED_CT),
            (CodeSystem.ICD10_CM, CODE_SYSTEM_ICD10_CM),
            (CodeSystem.RXNORM, CODE_SYSTEM_RXNORM),
        ],
    )
    def test_canonical_uri(self, system: CodeSystem, expected: str) -> None:
        assert system.canonical_uri == expected


# --------------------------------------------------------------------------- #
# loinc_map
# --------------------------------------------------------------------------- #
class TestLoincMap:
    def test_lookup_known_vital(self) -> None:
        assert lookup_loinc_display("8867-4") == "Heart rate"

    def test_lookup_known_lab(self) -> None:
        assert lookup_loinc_display("718-7").startswith("Hemoglobin")

    def test_lookup_unknown_code(self) -> None:
        assert lookup_loinc_display("99999-9") is None

    def test_lookup_empty(self) -> None:
        assert lookup_loinc_display("") is None

    def test_lookup_none(self) -> None:
        assert lookup_loinc_display(None) is None  # type: ignore[arg-type]

    def test_lookup_test_name_vital(self) -> None:
        assert lookup_loinc_test_name("8867-4") == "heart_rate"

    def test_lookup_test_name_alt_heart_rate(self) -> None:
        # 8893-0 is the alternate heart-rate code; both map to the same test.
        assert lookup_loinc_test_name("8893-0") == "heart_rate"

    def test_lookup_test_name_unknown(self) -> None:
        assert lookup_loinc_test_name("99999-9") is None

    def test_curated_map_nonempty(self) -> None:
        # Sanity: the map has enough codes to cover the safety engine.
        assert len(LOINC_DISPLAYS) >= 20

    def test_vital_loinc_codes_subset_of_test_map(self) -> None:
        # Every code flagged as a vital should also resolve to a test name —
        # otherwise the rule engine silently ignores a vital observation.
        from doctoragent.clinical.terminology.loinc_map import (
            LOINC_TO_REFERENCE_RANGES_TEST,
            VITAL_LOINC_CODES,
        )

        for code in VITAL_LOINC_CODES:
            assert code in LOINC_TO_REFERENCE_RANGES_TEST, (
                f"Vital LOINC code {code} not in test-name map"
            )


class TestLoincBulkLoader:
    def test_load_table_adds_new_codes(self, tmp_path, monkeypatch):
        # Build a TSV with a code the curated map doesn't have.
        path = tmp_path / "loinc.tsv"
        path.write_text("CODE\tDISPLAY\n99999-9\tCustom lab panel\n", encoding="utf-8")
        # Save + restore the curated map so the test is isolated.
        original = LOINC_DISPLAYS.get("99999-9")
        try:
            from doctoragent.clinical.terminology.loinc_map import load_loinc_table

            added = load_loinc_table(path)
            assert added == 1
            assert LOINC_DISPLAYS["99999-9"] == "Custom lab panel"
        finally:
            if original is None:
                LOINC_DISPLAYS.pop("99999-9", None)
            else:
                LOINC_DISPLAYS["99999-9"] = original

    def test_load_table_does_not_clobber_curated(self, tmp_path):
        # The curated entry for 8867-4 should win over the bulk file.
        path = tmp_path / "loinc.tsv"
        path.write_text(
            "CODE\tDISPLAY\n8867-4\tSHOULD NOT OVERRIDE\n", encoding="utf-8"
        )
        from doctoragent.clinical.terminology.loinc_map import load_loinc_table

        added = load_loinc_table(path)
        assert added == 0
        assert LOINC_DISPLAYS["8867-4"] == "Heart rate"

    def test_load_table_missing_columns(self, tmp_path):
        path = tmp_path / "bad.tsv"
        path.write_text("FOO\tBAR\n1\t2\n", encoding="utf-8")
        from doctoragent.clinical.terminology.loinc_map import load_loinc_table

        added = load_loinc_table(path)
        assert added == 0

    def test_load_table_nonexistent_path_returns_zero(self):
        from doctoragent.clinical.terminology.loinc_map import load_loinc_table

        assert load_loinc_table("/nonexistent/loinc.tsv") == 0

    def test_load_table_env_var(self, tmp_path, monkeypatch):
        path = tmp_path / "loinc.tsv"
        path.write_text("CODE\tDISPLAY\n88888-8\tEnv-loaded lab\n", encoding="utf-8")
        monkeypatch.setenv("DOCTORAGENT_TERMINOLOGY_LOINC_TABLE", str(path))
        original = LOINC_DISPLAYS.get("88888-8")
        try:
            from doctoragent.clinical.terminology.loinc_map import load_loinc_table

            assert load_loinc_table() == 1
            assert LOINC_DISPLAYS["88888-8"] == "Env-loaded lab"
        finally:
            if original is None:
                LOINC_DISPLAYS.pop("88888-8", None)
            else:
                LOINC_DISPLAYS["88888-8"] = original


# --------------------------------------------------------------------------- #
# icd10_map
# --------------------------------------------------------------------------- #
class TestIcd10Map:
    def test_lookup_known(self) -> None:
        assert lookup_icd10_display("E11.9") == (
            "Type 2 diabetes mellitus without complications"
        )

    def test_lookup_unknown(self) -> None:
        assert lookup_icd10_display("Z99.99") is None

    def test_lookup_empty(self) -> None:
        assert lookup_icd10_display("") is None

    def test_lookup_strips_whitespace(self) -> None:
        assert lookup_icd10_display("  I10  ") == (
            "Essential (primary) hypertension"
        )

    def test_curated_map_size(self) -> None:
        assert len(ICD10_DISPLAYS) >= 30


class TestIcd10FormatValidator:
    @pytest.mark.parametrize(
        "code",
        [
            "E11.9",
            "I10",
            "I48.91",
            "Z79.01",
            "S72.00",
            "F33.1",
            "N18.6",
        ],
    )
    def test_valid_codes(self, code: str) -> None:
        from doctoragent.clinical.terminology.icd10_map import is_valid_icd10_cm_format

        assert is_valid_icd10_cm_format(code)

    @pytest.mark.parametrize(
        "code",
        [
            "e11.9",  # lowercased prefix
            "E11-9",  # wrong separator
            "E11 9",  # space instead of dot
            "E11.9999",  # too many decimal places
            "E11.9X",  # trailing junk
            "111.9",  # missing alpha prefix
            "E1.9",  # only one digit
            "",  # empty
        ],
    )
    def test_invalid_codes(self, code: str) -> None:
        from doctoragent.clinical.terminology.icd10_map import is_valid_icd10_cm_format

        assert not is_valid_icd10_cm_format(code)

    def test_non_string_input(self) -> None:
        from doctoragent.clinical.terminology.icd10_map import is_valid_icd10_cm_format

        assert not is_valid_icd10_cm_format(None)  # type: ignore[arg-type]
        assert not is_valid_icd10_cm_format(123)  # type: ignore[arg-type]


class TestIcd10BulkLoader:
    def test_load_table_adds_new_codes(self, tmp_path):
        path = tmp_path / "icd10.csv"
        path.write_text(
            "CODE,DISPLAY\nZ99.99,Custom encounter reason\n", encoding="utf-8"
        )
        from doctoragent.clinical.terminology.icd10_map import load_icd10_table

        original = ICD10_DISPLAYS.get("Z99.99")
        try:
            added = load_icd10_table(path)
            assert added == 1
            assert ICD10_DISPLAYS["Z99.99"] == "Custom encounter reason"
        finally:
            if original is None:
                ICD10_DISPLAYS.pop("Z99.99", None)
            else:
                ICD10_DISPLAYS["Z99.99"] = original

    def test_load_table_does_not_clobber_curated(self, tmp_path):
        path = tmp_path / "icd10.csv"
        path.write_text("CODE,DISPLAY\nI10,SHOULD NOT OVERRIDE\n", encoding="utf-8")
        from doctoragent.clinical.terminology.icd10_map import load_icd10_table

        assert load_icd10_table(path) == 0
        assert ICD10_DISPLAYS["I10"] == "Essential (primary) hypertension"

    def test_load_table_missing_columns(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("FOO,BAR\n1,2\n", encoding="utf-8")
        from doctoragent.clinical.terminology.icd10_map import load_icd10_table

        assert load_icd10_table(path) == 0

    def test_load_table_env_var(self, tmp_path, monkeypatch):
        path = tmp_path / "icd10.csv"
        path.write_text(
            "CODE,DISPLAY\nW99.99,Env-loaded code\n", encoding="utf-8"
        )
        monkeypatch.setenv("DOCTORAGENT_TERMINOLOGY_ICD10_TABLE", str(path))
        from doctoragent.clinical.terminology.icd10_map import load_icd10_table

        original = ICD10_DISPLAYS.get("W99.99")
        try:
            assert load_icd10_table() == 1
            assert ICD10_DISPLAYS["W99.99"] == "Env-loaded code"
        finally:
            if original is None:
                ICD10_DISPLAYS.pop("W99.99", None)
            else:
                ICD10_DISPLAYS["W99.99"] = original


# --------------------------------------------------------------------------- #
# snowstorm
# --------------------------------------------------------------------------- #
def _snowstorm_concept_payload(
    concept_id: str = "763158003",
    pt: str = "Medicinal product (product)",
    fsn: str = "Medicinal product (product)",
    active: bool = True,
) -> dict[str, Any]:
    """Build a minimal Snowstorm concept JSON payload."""
    return {
        "conceptId": concept_id,
        "pt": {"term": pt},
        "fsn": {"term": fsn},
        "active": active,
        "moduleId": "900000000000207008",
        "definitionStatus": {"conceptId": "900000000000074008", "term": "FULLY_DEFINED"},
    }


def _snowstorm_handler(
    concept_payload: dict[str, Any] | None = None,
    ancestors: list[str] | None = None,
    concept_status: int = 200,
    ancestors_status: int = 200,
) -> httpx.MockTransport:
    """Build a MockTransport emulating Snowstorm endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/concepts/" in url and "/ancestors" not in url:
            if concept_status != 200:
                return httpx.Response(concept_status, text="error")
            return httpx.Response(200, json=concept_payload or _snowstorm_concept_payload())
        if "/ancestors" in url:
            if ancestors_status != 200:
                return httpx.Response(ancestors_status, text="error")
            payload = ancestors if ancestors is not None else ["138875005", "373873005"]
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestSnowstormClient:
    async def test_lookup_happy_path(self) -> None:
        transport = _snowstorm_handler()
        async with SnowstormClient(transport=transport) as client:
            concept = await client.lookup("763158003")
        assert concept.concept_id == "763158003"
        assert concept.preferred_term == "Medicinal product (product)"
        assert concept.active is True
        assert concept.module_id == "900000000000207008"

    async def test_lookup_404_raises_not_found(self) -> None:
        transport = _snowstorm_handler(concept_status=404)
        async with SnowstormClient(transport=transport) as client:
            with pytest.raises(SnowstormNotFoundError):
                await client.lookup("9999999999")

    async def test_lookup_500_raises_snowstorm_error(self) -> None:
        transport = _snowstorm_handler(concept_status=500)
        async with SnowstormClient(transport=transport) as client:
            with pytest.raises(SnowstormError):
                await client.lookup("763158003")

    async def test_lookup_non_json_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json")

        async with SnowstormClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(SnowstormError):
                await client.lookup("763158003")

    async def test_get_preferred_term(self) -> None:
        transport = _snowstorm_handler()
        async with SnowstormClient(transport=transport) as client:
            pt = await client.get_preferred_term("763158003")
        assert pt == "Medicinal product (product)"

    async def test_get_ancestors_list_payload(self) -> None:
        transport = _snowstorm_handler(ancestors=["138875005", "373873005"])
        async with SnowstormClient(transport=transport) as client:
            ancestors = await client.get_ancestors("763158003")
        assert "373873005" in ancestors

    async def test_get_ancestors_dict_payload(self) -> None:
        # Some deployments wrap the list in {"ancestors": [...]}.
        def handler(request: httpx.Request) -> httpx.Response:
            if "/ancestors" in str(request.url):
                return httpx.Response(200, json={"ancestors": ["1", "2"]})
            return httpx.Response(200, json=_snowstorm_concept_payload())

        async with SnowstormClient(transport=httpx.MockTransport(handler)) as client:
            ancestors = await client.get_ancestors("763158003")
        assert ancestors == ["1", "2"]

    async def test_lookup_with_hierarchy_populates_ancestors(self) -> None:
        transport = _snowstorm_handler(ancestors=["138875005", "373873005"])
        async with SnowstormClient(transport=transport) as client:
            concept = await client.lookup_with_hierarchy("763158003")
        assert concept.ancestors == ["138875005", "373873005"]
        assert concept.is_a("373873005")

    async def test_is_a_true(self) -> None:
        transport = _snowstorm_handler(ancestors=["138875005", "373873005"])
        async with SnowstormClient(transport=transport) as client:
            assert await client.is_a("763158003", "373873005")

    async def test_is_a_false_when_not_ancestor(self) -> None:
        transport = _snowstorm_handler(ancestors=["138875005"])
        async with SnowstormClient(transport=transport) as client:
            assert not await client.is_a("763158003", "373873005")

    async def test_is_a_false_on_404(self) -> None:
        transport = _snowstorm_handler(ancestors_status=404)
        async with SnowstormClient(transport=transport) as client:
            assert not await client.is_a("9999999999", "373873005")

    async def test_lookup_with_hierarchy_tolerates_missing_ancestors(self) -> None:
        # Some editions don't expose /ancestors — the concept is still returned.
        transport = _snowstorm_handler(ancestors_status=404)
        async with SnowstormClient(transport=transport) as client:
            concept = await client.lookup_with_hierarchy("763158003")
        assert concept.concept_id == "763158003"
        assert concept.ancestors == []

    async def test_close_releases_client(self) -> None:
        transport = _snowstorm_handler()
        client = SnowstormClient(transport=transport)
        await client.aclose()
        # Calling aclose twice is safe.
        await client.aclose()

    def test_invalid_base_url_raises(self) -> None:
        with pytest.raises(ValueError):
            SnowstormClient(base_url="")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            SnowstormClient(base_url=None)  # type: ignore[arg-type]

    async def test_invalid_concept_id_raises(self) -> None:
        transport = _snowstorm_handler()
        async with SnowstormClient(transport=transport) as client:
            with pytest.raises(ValueError):
                await client.lookup("")


# --------------------------------------------------------------------------- #
# TerminologyService (façade)
# --------------------------------------------------------------------------- #
class TestTerminologyServiceSync:
    """Sync-path lookups against the curated maps."""

    def setup_method(self) -> None:
        # No Snowstorm / RxNorm client — sync-only path.
        self.svc = TerminologyService(load_tables=False)

    def test_loinc_lookup(self) -> None:
        result = self.svc.lookup_display("http://loinc.org", "8867-4")
        assert result.found
        assert result.display == "Heart rate"
        assert result.code_system == CodeSystem.LOINC
        assert result.source == "loinc_curated"

    def test_loinc_lookup_unknown_code_with_fallback(self) -> None:
        result = self.svc.lookup_display(
            "http://loinc.org", "99999-9", fallback_display="Custom panel"
        )
        # No curated entry → fallback to the resource's own display.
        assert result.display == "Custom panel"
        assert result.source == "resource_display"

    def test_loinc_lookup_unknown_code_no_fallback(self) -> None:
        result = self.svc.lookup_display("http://loinc.org", "99999-9")
        assert result.display is None
        assert not result.found

    def test_icd10_lookup(self) -> None:
        result = self.svc.lookup_display(
            "http://hl7.org/fhir/sid/icd-10-cm", "E11.9"
        )
        assert result.display == "Type 2 diabetes mellitus without complications"
        assert result.code_system == CodeSystem.ICD10_CM
        assert result.source == "icd10_curated"

    def test_icd10_valid_format_not_in_map(self) -> None:
        # Structurally valid but not curated → "valid-but-unknown" provenance.
        result = self.svc.lookup_display(
            "http://hl7.org/fhir/sid/icd-10-cm", "Z99.99"
        )
        assert result.display is None
        assert result.extra.get("format_valid") is True
        assert result.extra.get("in_curated_map") is False

    def test_icd10_invalid_format(self) -> None:
        result = self.svc.lookup_display(
            "http://hl7.org/fhir/sid/icd-10-cm", "not-a-code"
        )
        assert result.display is None
        # No format_valid extra → structurally invalid.
        assert "format_valid" not in result.extra

    def test_snomed_sync_falls_back_to_display(self) -> None:
        # Sync path can't hit Snowstorm — graceful degradation.
        result = self.svc.lookup_display(
            "http://snomed.info/sct",
            "763158003",
            fallback_display="Medicinal product",
        )
        assert result.display == "Medicinal product"
        assert result.code_system == CodeSystem.SNOMED_CT
        assert result.source == "resource_display"
        assert result.extra.get("requires_async_lookup") is True

    def test_rxnorm_sync_falls_back(self) -> None:
        result = self.svc.lookup_display(
            CODE_SYSTEM_RXNORM, "12345", fallback_display="Aspirin"
        )
        assert result.display == "Aspirin"
        assert result.code_system == CodeSystem.RXNORM

    def test_unknown_system(self) -> None:
        result = self.svc.lookup_display("http://example.com/x", "abc")
        assert result.code_system == CodeSystem.UNKNOWN
        assert result.display is None

    def test_empty_code(self) -> None:
        result = self.svc.lookup_display("http://loinc.org", "")
        assert result.code == ""
        assert result.code_system == CodeSystem.UNKNOWN

    def test_loinc_to_test_name(self) -> None:
        assert self.svc.loinc_to_test_name("8867-4") == "heart_rate"
        assert self.svc.loinc_to_test_name("99999-9") is None


class TestTerminologyServiceResolveCoding:
    """FHIR Coding / CodeableConcept convenience resolvers."""

    def setup_method(self) -> None:
        self.svc = TerminologyService(load_tables=False)

    def test_resolve_coding_loinc(self) -> None:
        coding = {"system": "http://loinc.org", "code": "8867-4", "display": "HR"}
        result = self.svc.resolve_coding(coding)
        assert result.display == "Heart rate"
        assert result.source == "loinc_curated"

    def test_resolve_coding_unknown_system_uses_display(self) -> None:
        coding = {
            "system": "http://example.com/unknown",
            "code": "abc",
            "display": "Custom display",
        }
        result = self.svc.resolve_coding(coding)
        assert result.display == "Custom display"

    def test_resolve_coding_no_display(self) -> None:
        coding = {"system": "http://loinc.org", "code": "99999-9"}
        result = self.svc.resolve_coding(coding)
        assert result.display is None

    def test_resolve_coding_non_dict(self) -> None:
        result = self.svc.resolve_coding("not a dict")  # type: ignore[arg-type]
        assert result.display is None
        assert result.code_system == CodeSystem.UNKNOWN

    def test_resolve_codeable_concept_text_wins(self) -> None:
        cc = {
            "text": "Curated text label",
            "coding": [{"system": "http://loinc.org", "code": "8867-4"}],
        }
        result = self.svc.resolve_codeable_concept(cc)
        # The CodeableConcept.text is the human-curated label — it wins.
        assert result.display == "Curated text label"

    def test_resolve_codeable_concept_first_coding(self) -> None:
        cc = {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]}
        result = self.svc.resolve_codeable_concept(cc)
        assert result.display == "Heart rate"

    def test_resolve_codeable_concept_unknown_first_known_second(self) -> None:
        cc = {
            "coding": [
                {"system": "http://loinc.org", "code": "99999-9", "display": "FB"},
                {"system": "http://loinc.org", "code": "8867-4"},
            ]
        }
        result = self.svc.resolve_codeable_concept(cc)
        # First coding unknown → second coding resolves.
        assert result.display == "Heart rate"

    def test_resolve_codeable_concept_empty_coding(self) -> None:
        cc = {"coding": []}
        result = self.svc.resolve_codeable_concept(cc)
        assert result.display is None

    def test_resolve_codeable_concept_non_dict(self) -> None:
        result = self.svc.resolve_codeable_concept(None)  # type: ignore[arg-type]
        assert result.display is None


class TestTerminologyServiceAsync:
    """Async-path lookups hitting Snowstorm / RxNorm (mocked)."""

    async def test_lookup_display_async_loinc_same_as_sync(self) -> None:
        svc = TerminologyService(load_tables=False)
        result = await svc.lookup_display_async("http://loinc.org", "8867-4")
        assert result.display == "Heart rate"
        assert result.source == "loinc_curated"

    async def test_lookup_display_async_snomed_with_client(self) -> None:
        transport = _snowstorm_handler()
        async with SnowstormClient(transport=transport) as sc:
            svc = TerminologyService(snowstorm_client=sc, load_tables=False)
            result = await svc.lookup_display_async(
                "http://snomed.info/sct", "763158003"
            )
        assert result.display == "Medicinal product (product)"
        assert result.source == "snomed_snowstorm"
        assert result.code_system == CodeSystem.SNOMED_CT

    async def test_lookup_display_async_snomed_no_client_falls_back(self) -> None:
        svc = TerminologyService(load_tables=False)
        result = await svc.lookup_display_async(
            "http://snomed.info/sct",
            "763158003",
            fallback_display="Medicinal product",
        )
        assert result.display == "Medicinal product"
        assert result.extra.get("requires_snowstorm_client") is True

    async def test_lookup_display_async_snomed_not_found(self) -> None:
        transport = _snowstorm_handler(concept_status=404)
        async with SnowstormClient(transport=transport) as sc:
            svc = TerminologyService(snowstorm_client=sc, load_tables=False)
            result = await svc.lookup_display_async(
                "http://snomed.info/sct",
                "9999999999",
                fallback_display="FB",
            )
        assert result.display == "FB"
        assert result.extra.get("not_found") is True

    async def test_lookup_display_async_snomed_server_error_falls_back(self) -> None:
        transport = _snowstorm_handler(concept_status=500)
        async with SnowstormClient(transport=transport) as sc:
            svc = TerminologyService(snowstorm_client=sc, load_tables=False)
            result = await svc.lookup_display_async(
                "http://snomed.info/sct",
                "763158003",
                fallback_display="FB",
            )
        assert result.display == "FB"
        assert result.extra.get("snowstorm_error") is True

    async def test_lookup_snomed_concept_requires_client(self) -> None:
        svc = TerminologyService(load_tables=False)
        with pytest.raises(TerminologyServiceError):
            await svc.lookup_snomed_concept("763158003")

    async def test_lookup_snomed_concept_success(self) -> None:
        transport = _snowstorm_handler(ancestors=["1", "2"])
        async with SnowstormClient(transport=transport) as sc:
            svc = TerminologyService(snowstorm_client=sc, load_tables=False)
            concept = await svc.lookup_snomed_concept("763158003")
        assert concept.concept_id == "763158003"
        assert concept.ancestors == ["1", "2"]

    async def test_lookup_snomed_concept_not_found_propagates(self) -> None:
        transport = _snowstorm_handler(concept_status=404)
        async with SnowstormClient(transport=transport) as sc:
            svc = TerminologyService(snowstorm_client=sc, load_tables=False)
            with pytest.raises(SnowstormNotFoundError):
                await svc.lookup_snomed_concept("9999999999")

    async def test_is_a_true(self) -> None:
        transport = _snowstorm_handler(ancestors=["373873005"])
        async with SnowstormClient(transport=transport) as sc:
            svc = TerminologyService(snowstorm_client=sc, load_tables=False)
            assert await svc.is_a("763158003", "373873005")

    async def test_is_a_false_when_no_client(self) -> None:
        svc = TerminologyService(load_tables=False)
        assert not await svc.is_a("763158003", "373873005")

    async def test_is_a_false_on_error(self) -> None:
        transport = _snowstorm_handler(ancestors_status=500)
        async with SnowstormClient(transport=transport) as sc:
            svc = TerminologyService(snowstorm_client=sc, load_tables=False)
            # Should never raise — graceful degradation.
            assert not await svc.is_a("763158003", "373873005")

    async def test_rxnorm_lookup_with_client(self) -> None:
        # Mock the RxNormClient.get_drug_info coroutine.
        mock_rxnorm = AsyncMock()
        mock_rxnorm.get_drug_info = AsyncMock(
            return_value={
                "rxcui": "161",
                "name": "Acetaminophen",
                "tty": "IN",
                "brand_names": ["Tylenol"],
            }
        )
        svc = TerminologyService(rxnorm_client=mock_rxnorm, load_tables=False)
        result = await svc.lookup_display_async(CODE_SYSTEM_RXNORM, "161")
        assert result.display == "Acetaminophen"
        assert result.source == "rxnorm_api"
        assert result.extra.get("tty") == "IN"

    async def test_rxnorm_lookup_no_client_falls_back(self) -> None:
        svc = TerminologyService(load_tables=False)
        result = await svc.lookup_display_async(
            CODE_SYSTEM_RXNORM, "161", fallback_display="Aspirin"
        )
        assert result.display == "Aspirin"
        assert result.extra.get("requires_rxnorm_client") is True

    async def test_rxnorm_lookup_not_found(self) -> None:
        mock_rxnorm = AsyncMock()
        mock_rxnorm.get_drug_info = AsyncMock(return_value={"name": ""})
        svc = TerminologyService(rxnorm_client=mock_rxnorm, load_tables=False)
        result = await svc.lookup_display_async(
            CODE_SYSTEM_RXNORM, "999", fallback_display="FB"
        )
        assert result.display == "FB"
        assert result.extra.get("not_found") is True

    async def test_rxnorm_lookup_error_falls_back(self) -> None:
        mock_rxnorm = AsyncMock()
        mock_rxnorm.get_drug_info = AsyncMock(side_effect=RuntimeError("boom"))
        svc = TerminologyService(rxnorm_client=mock_rxnorm, load_tables=False)
        result = await svc.lookup_display_async(
            CODE_SYSTEM_RXNORM, "161", fallback_display="FB"
        )
        assert result.display == "FB"
        assert result.extra.get("rxnorm_error") is True

    async def test_aclose_closes_snowstorm(self) -> None:
        transport = _snowstorm_handler()
        sc = SnowstormClient(transport=transport)
        svc = TerminologyService(snowstorm_client=sc, load_tables=False)
        await svc.aclose()
        # Verify the underlying client is closed.
        assert sc._client.is_closed

    async def test_async_context_manager(self) -> None:
        transport = _snowstorm_handler()
        sc = SnowstormClient(transport=transport)
        async with TerminologyService(snowstorm_client=sc, load_tables=False) as svc:
            result = await svc.lookup_display_async(
                "http://snomed.info/sct", "763158003"
            )
        assert result.display == "Medicinal product (product)"
        assert sc._client.is_closed


# --------------------------------------------------------------------------- #
# Module-level default service + free function
# --------------------------------------------------------------------------- #
class TestDefaultService:
    def test_get_default_service_singleton(self) -> None:
        from doctoragent.clinical.terminology.service import (
            configure_default_service,
            get_default_service,
        )

        # Reset to a fresh service.
        configure_default_service(TerminologyService(load_tables=False))
        s1 = get_default_service()
        s2 = get_default_service()
        assert s1 is s2

    def test_lookup_display_free_function(self) -> None:
        from doctoragent.clinical.terminology.service import (
            configure_default_service,
        )

        configure_default_service(TerminologyService(load_tables=False))
        assert lookup_display("http://loinc.org", "8867-4") == "Heart rate"
        assert lookup_display("http://loinc.org", "99999-9") is None
        # Fallback display when no curated entry.
        assert lookup_display(
            "http://loinc.org", "99999-9", fallback_display="FB"
        ) == "FB"


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #
class TestTerminologyServiceResult:
    def test_found_true_when_display_present(self) -> None:
        r = TerminologyServiceResult(
            code="8867-4",
            code_system=CodeSystem.LOINC,
            display="Heart rate",
            source="loinc_curated",
        )
        assert r.found

    def test_found_false_when_display_none(self) -> None:
        r = TerminologyServiceResult(
            code="99999-9",
            code_system=CodeSystem.LOINC,
            display=None,
            source="unknown",
        )
        assert not r.found

    def test_found_false_when_display_empty(self) -> None:
        r = TerminologyServiceResult(
            code="99999-9",
            code_system=CodeSystem.LOINC,
            display="",
            source="unknown",
        )
        assert not r.found

    def test_equality(self) -> None:
        r1 = TerminologyServiceResult(
            code="8867-4",
            code_system=CodeSystem.LOINC,
            display="Heart rate",
            source="loinc_curated",
        )
        r2 = TerminologyServiceResult(
            code="8867-4",
            code_system=CodeSystem.LOINC,
            display="Heart rate",
            source="loinc_curated",
        )
        assert r1 == r2

    def test_inequality(self) -> None:
        r1 = TerminologyServiceResult(
            code="8867-4",
            code_system=CodeSystem.LOINC,
            display="Heart rate",
            source="loinc_curated",
        )
        r2 = TerminologyServiceResult(
            code="8867-4",
            code_system=CodeSystem.LOINC,
            display="Heart rate",
            source="snomed_snowstorm",  # different source
        )
        assert r1 != r2

    def test_repr(self) -> None:
        r = TerminologyServiceResult(
            code="8867-4",
            code_system=CodeSystem.LOINC,
            display="Heart rate",
            source="loinc_curated",
        )
        assert "8867-4" in repr(r)
        assert "loinc" in repr(r)

    def test_extra_default_empty(self) -> None:
        r = TerminologyServiceResult(
            code="x", code_system=CodeSystem.UNKNOWN, display=None, source="x"
        )
        assert r.extra == {}


# --------------------------------------------------------------------------- #
# Integration: terminology binding across the CDS Hooks translator
# --------------------------------------------------------------------------- #
class TestCdsHooksIntegrationWithTerminology:
    """The CDS Hooks translator now imports from the terminology package.

    Verify the integration by checking that the LOINC code → test-name
    mapping the translator uses is the same map the terminology package
    exposes (no drift between layers).
    """

    def test_translator_uses_terminology_loinc_map(self) -> None:
        # The translator no longer carries its own _LOINC_TO_TEST_NAME; the
        # terminology package's map is the single source of truth.
        from doctoragent.clinical.integrations.cds_hooks import service as cds_service
        from doctoragent.clinical.terminology.loinc_map import (
            LOINC_TO_REFERENCE_RANGES_TEST,
        )

        # The duplicated private map should be gone.
        assert not hasattr(cds_service, "_LOINC_TO_TEST_NAME")
        assert not hasattr(cds_service, "_VITAL_LOINC_CODES")
        # The translator's _extract_vitals_labs function must produce the
        # same test-name mapping the terminology package owns.
        sample_obs = [
            {
                "resourceType": "Observation",
                "id": "obs-1",
                "code": {
                    "coding": [
                        {"system": "http://loinc.org", "code": "8867-4"},
                    ]
                },
                "valueQuantity": {"value": 35, "unit": "bpm"},
                "category": [
                    {
                        "coding": [{"code": "vital-signs"}],
                    }
                ],
            }
        ]
        vitals, labs = cds_service._extract_vitals_labs(sample_obs)
        # 8867-4 → heart_rate per the terminology package.
        assert "heart_rate" in vitals
        assert vitals["heart_rate"] == 35
        # Sanity: the code is in the terminology package's map.
        assert LOINC_TO_REFERENCE_RANGES_TEST["8867-4"] == "heart_rate"
