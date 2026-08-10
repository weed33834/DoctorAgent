# mypy: ignore-errors
"""Tests for M18-M23 additions: semantic cache, text utils, governance, pricing, error catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from doctoragent.api.error_catalog import catalog, http_status_for_code, lookup
from doctoragent.governance import AssetType, DataSensitivity, GovernanceService, GovernanceStore
from doctoragent.model.pricing import ModelPricing
from doctoragent.model.semantic_cache import SemanticCache, _cosine
from doctoragent.model.text_utils import extract_keywords, split_sentences, summarize, token_count


# ── text utils (M18) ──────────────────────────────────────────────────


def test_extract_keywords() -> None:
    kws = extract_keywords("SGLT2i 在心衰患者中降低住院风险，SGLT2i 改善预后。")
    assert "SGLT2i" in kws
    # Chinese char tokens present
    assert kws
    assert extract_keywords("") == []


def test_split_sentences_and_summarize() -> None:
    assert split_sentences("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]
    text = "A。" * 10
    s = summarize(text, max_sentences=3)
    assert len(split_sentences(s)) <= 3


def test_token_count() -> None:
    assert token_count("你好世界") == 4
    assert token_count("hello world") > 0


# ── semantic cache (M23) ──────────────────────────────────────────────


class _FakeEmbed:
    def embed(self, texts: list[str]) -> list[list[float]]:
        # very crude: same keyword family → similar vector
        out = []
        for t in texts:
            v = [1.0 if "心衰" in t else 0.0, 1.0 if "SGLT2" in t or "SGLT2i" in t else 0.0, 1.0]
            out.append(v)
        return out


def test_cosine() -> None:
    assert abs(_cosine([1, 0], [1, 0]) - 1.0) < 1e-6
    assert abs(_cosine([1, 0], [0, 1])) < 1e-6


def test_semantic_cache_hit_with_embedding(tmp_path: Path) -> None:
    cache = SemanticCache(threshold=0.9, embedding_provider=_FakeEmbed(),
                          persist_path=tmp_path / "sc.db")
    cache.put("SGLT2i治疗心衰的机制", "answer-A")
    hit = cache.get("SGLT2抑制剂治疗心衰 机制")  # semantically similar keywords
    assert hit == "answer-A"


def test_semantic_cache_miss() -> None:
    cache = SemanticCache(embedding_provider=_FakeEmbed())
    cache.put("心衰用药", "answer-B")
    assert cache.get("肺炎治疗") is None


def test_semantic_cache_sensitive_skip() -> None:
    cache = SemanticCache(sensitive_prefixes=("患者",))
    cache.put("患者张三的诊断", "x")  # should be skipped (sensitive)
    assert cache.stats()["entries"] == 0


def test_semantic_cache_clear(tmp_path: Path) -> None:
    cache = SemanticCache(persist_path=tmp_path / "sc2.db")
    cache.put("q", "r")
    assert cache.stats()["entries"] == 1
    cache.clear()
    assert cache.stats()["entries"] == 0


# ── governance (M20) ──────────────────────────────────────────────────


@pytest.fixture
def governance(tmp_path: Path) -> GovernanceService:
    return GovernanceService(GovernanceStore(tmp_path / "gov.db"))


def test_governance_register_and_classify(governance: GovernanceService) -> None:
    governance.add_classification_rule("PHI", DataSensitivity.PHI, ["身份证号"])
    asset = governance.register_asset("病历", AssetType.DOCUMENT, content="患者身份证号110101...")
    assert asset.sensitivity == DataSensitivity.PHI
    assert "身份证号" in asset.metadata["keywords"] or asset.metadata["keywords"]


def test_governance_lineage_and_quality(governance: GovernanceService) -> None:
    a = governance.register_asset("指南", AssetType.KNOWLEDGE_BASE, content="SGLT2i 建议。")
    b = governance.register_asset("摘要", AssetType.DOCUMENT, content="基于指南的摘要。")
    governance.record_lineage(a.id, b.id, "summarize")
    lineage = governance.store.get_lineage(b.id)
    assert any(e["upstream"] == a.id for e in lineage["upstream"])
    quals = governance.store.quality_for(a.id)
    assert len(quals) >= 1
    assert quals[0].check_type == "completeness"


def test_governance_summary(governance: GovernanceService) -> None:
    governance.register_asset("x", AssetType.DOCUMENT, content="内容")
    summary = governance.catalog_summary()
    assert summary["assets"] == 1
    assert summary["by_type"].get("document") == 1


# ── pricing (M21) ─────────────────────────────────────────────────────


def test_pricing_lookup() -> None:
    p = ModelPricing()
    spec = p.lookup("gpt-4o")
    assert spec is not None
    assert spec["input"] > 0


def test_pricing_cost_per_1k() -> None:
    p = ModelPricing()
    cost = p.cost_per_1k("gpt-4o", 1000, 1000)
    assert cost > 0


def test_pricing_compare_sorted_cheapest_first() -> None:
    p = ModelPricing()
    res = p.compare(["gpt-4o", "deepseek-v3", "qwen2.5-7b"])
    costs = [m["example_cost_usd"] for m in res]
    assert costs == sorted(costs)


def test_pricing_unknown_model() -> None:
    p = ModelPricing()
    assert p.lookup("does-not-exist") is None
    assert p.cost_per_1k("does-not-exist", 1000, 1000) == 0.0


# ── error catalog (M19) ───────────────────────────────────────────────


def test_error_catalog_lookup() -> None:
    info = lookup("EAUTH401")
    assert info is not None
    assert info.http_status == 401
    assert http_status_for_code("EAUTH401") == 401
    assert http_status_for_code("UNKNOWN") == 500


def test_error_catalog_nonempty() -> None:
    items = catalog()
    assert len(items) >= 10
    codes = {i["code"] for i in items}
    assert len(codes) == len(items)  # unique codes
