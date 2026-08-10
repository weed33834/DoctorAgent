# mypy: ignore-errors
"""Tests for long-horizon memory consolidation (episodic → semantic compaction)."""

from __future__ import annotations

from pathlib import Path

import pytest

from doctoragent.model.rag import MemorySystem, _split_fact_candidates


@pytest.fixture
def memory(tmp_path: Path) -> MemorySystem:
    return MemorySystem(tmp_path / "memory.db")


def test_split_fact_candidates() -> None:
    parts = _split_fact_candidates("心衰患者使用SGLT2i有获益。建议监测肾功能。")
    assert parts == ["心衰患者使用SGLT2i有获益。", "建议监测肾功能。"]
    assert _split_fact_candidates("") == []


def test_consolidate_adds_semantic_facts(memory: MemorySystem) -> None:
    memory.store_episode(
        "s1",
        "心衰治疗",
        "SGLT2i在HFrEF中降低住院风险。",
        key_facts=["SGLT2i降低HFrEF住院风险"],
    )
    memory.store_episode(
        "s2",
        "用药",
        "华法林与布洛芬合用增加出血风险。",
        key_facts=["华法林+布洛芬增加出血风险"],
    )

    stats = memory.consolidate_memories()
    assert stats["episodes_consolidated"] == 2
    assert stats["facts_added"] >= 2

    facts = memory.recall_facts("出血风险", limit=10)
    contents = [f.content for f in facts]
    assert any("华法林" in c for c in contents)
    assert all(f.memory_type == "semantic" for f in facts if f.content == "华法林+布洛芬增加出血风险")


def test_consolidation_is_idempotent(memory: MemorySystem) -> None:
    memory.store_episode("s1", "q", "A。B。", key_facts=["F1", "F2"])
    first = memory.consolidate_memories()
    second = memory.consolidate_memories()
    assert first["episodes_consolidated"] == 1
    assert second["episodes_considered"] == 0
    assert second["facts_added"] == 0


def test_consolidate_dedupes_against_existing_facts(memory: MemorySystem) -> None:
    memory.store_fact("已知事实A", memory_type="semantic", importance=0.9)
    memory.store_episode("s1", "q", "已知事实A。", key_facts=["已知事实A"])
    stats = memory.consolidate_memories()
    # The duplicate fact should be skipped (dedup), not re-added.
    assert stats["facts_skipped"] >= 1


def test_consolidate_with_custom_extractor(memory: MemorySystem) -> None:
    def extractor(_u: str, _a: str, _c: str) -> list[str]:
        return ["自定义提取事实", "第二条事实"]

    memory.store_episode("s1", "q", "任何内容", key_facts=["忽略的key"])
    stats = memory.consolidate_memories(extractor=extractor)
    assert stats["facts_added"] == 2
    contents = [f.content for f in memory.recall_facts("自定义", limit=10)]
    assert "自定义提取事实" in contents


def test_store_episode_auto_triggers_consolidation(memory: MemorySystem) -> None:
    # Force a tiny interval so storing episodes trips the periodic pass.
    memory._consolidation_interval = 3
    for i in range(3):
        memory.store_episode(f"s{i}", "q", f"事实{i}。", key_facts=[f"KF{i}"])
    facts = memory.recall_facts("事实", limit=10)
    assert len(facts) >= 1  # at least one episode was compacted into a fact
