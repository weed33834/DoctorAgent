# mypy: ignore-errors
"""Tests for M26 (multimodal), M28 (pipeline), M14 KB/task-center, M6.19 debate, M3.20 adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from doctoragent.agent.adapters import create_adapter
from doctoragent.datapipeline import PipelineService, PipelineStore
from doctoragent.knowledge_base import KnowledgeBaseManager
from doctoragent.multimodal import MultimodalService, MultimodalStore
from doctoragent.orchestration.group_chat import run_debate
from doctoragent.taskcenter import TaskCenter
from doctoragent.tools.image_gen_tool import ImageGenTool


# ── M26 multimodal ────────────────────────────────────────────────────


@pytest.fixture
def mm(tmp_path: Path) -> MultimodalService:
    return MultimodalService(MultimodalStore(tmp_path / "mm.db"))


def test_mm_add_and_search(mm: MultimodalService) -> None:
    mm.ingest("音频1", "audio", path="", extracted_text="患者主诉胸痛")
    mm.ingest("图1", "image", path="", extracted_text="胸痛心电图 ST 抬高")
    res = mm.search("胸痛")
    assert res["total"] >= 2
    assert res["hits"][0]["modality"] in ("audio", "image")


def test_mm_invalid_modality(mm: MultimodalService) -> None:
    with pytest.raises(ValueError):
        mm.ingest("x", "hologram")


def test_mm_summary(mm: MultimodalService) -> None:
    mm.ingest("a", "text", path="", extracted_text="内容")
    s = mm.store.summary()
    assert s["assets"] == 1
    assert s["by_modality"].get("text") == 1


# ── M28 data pipeline ─────────────────────────────────────────────────


@pytest.fixture
def pipe(tmp_path: Path) -> PipelineService:
    return PipelineService(PipelineStore(tmp_path / "pipe.db"))


def test_pipeline_run_dedupe_filter(pipe: PipelineService) -> None:
    pipe.store.add_source("csv", "csv")
    pipe.store.add_pipeline("清洗", ["filter_empty", "dedupe"])
    pid = pipe.store.list_pipelines()[0]["id"]
    run = pipe.run_pipeline(pid, batch=[{"text": "A"}, {"text": "A"}, {"text": ""}])
    assert run["records_processed"] == 1
    assert run["status"] == "completed"


def test_pipeline_transform_rules(pipe: PipelineService) -> None:
    pipe.store.add_transform_rule("lower", "*", "lowercase")
    assert len(pipe.store.list_transform_rules()) == 1


def test_pipeline_quality(pipe: PipelineService) -> None:
    pipe.store.add_pipeline("p", [])
    pid = pipe.store.list_pipelines()[0]["id"]
    pipe.run_pipeline(pid, batch=[{"text": "x"}])
    q = pipe.store.list_quality(pid)
    assert len(q) == 1
    assert q[0]["check_type"] == "completeness"


def test_pipeline_overview(pipe: PipelineService) -> None:
    pipe.store.add_source("s", "db")
    ov = pipe.overview()
    assert ov["sources"] == 1


# ── M14 knowledge base manager ────────────────────────────────────────


@pytest.fixture
def kb(tmp_path: Path) -> KnowledgeBaseManager:
    vault = tmp_path / "Vault"
    vault.mkdir()
    return KnowledgeBaseManager(tmp_path / "kb.db", vault)


def test_kb_create_list_update(kb: KnowledgeBaseManager) -> None:
    k = kb.create("指南库", embedding_model="default")
    assert (kb.vault_root / k["dir_name"]).is_dir()
    assert len(kb.list()) == 1
    kb.update(k["id"], chunk_size=300)
    assert kb.get(k["id"])["chunk_size"] == 300


def test_kb_test_retrieval(kb: KnowledgeBaseManager) -> None:
    k = kb.create("库")
    d = kb.vault_root / k["dir_name"]
    (d / "心衰指南.pdf").write_text("x")
    (d / "肺炎指南.pdf").write_text("x")
    res = kb.test_retrieval(k["id"], "心衰")
    assert res["files"] == 2
    assert res["results"][0]["name"] == "心衰指南.pdf"


def test_kb_delete(kb: KnowledgeBaseManager) -> None:
    k = kb.create("库")
    kb.delete(k["id"])
    assert kb.get(k["id"]) is None


def test_kb_summary(kb: KnowledgeBaseManager) -> None:
    kb.create("库A", visibility="private")
    s = kb.summary()
    assert s["knowledge_bases"] == 1


# ── M14 task center ───────────────────────────────────────────────────


@pytest.fixture
def tc(tmp_path: Path) -> TaskCenter:
    return TaskCenter(tmp_path / "tasks.db")


def test_task_center_create_and_retry(tc: TaskCenter) -> None:
    tc.register_handler("backup", lambda params: "backup-ok")
    t = tc.create("backup", "每日备份", {"scope": "db"})
    assert t["status"] in ("completed", "pending")
    assert len(tc.list()) >= 1
    assert tc.summary()["total"] >= 1


def test_task_center_unknown_type(tc: TaskCenter) -> None:
    with pytest.raises(ValueError):
        tc.create("nope", "x")


# ── M6.19 debate ──────────────────────────────────────────────────────


def test_debate_runs_and_verdict() -> None:
    def pro(p: str, c: dict) -> str:
        return "支持，有依据"
    def con(p: str, c: dict) -> str:
        return "反对，有风险"
    def judge(p: str, c: dict) -> str:
        return "综合双方，裁定支持"
    result = run_debate("该不该用X药", pro, con, judge, rounds=2)
    assert result["rounds"] == 2
    assert result["verdict"] == "综合双方，裁定支持"
    assert len(result["transcript"]) == 5  # 2 rounds * 2 + judge


# ── M3.20 adapters ────────────────────────────────────────────────────


def test_adk_and_autogen_adapter_names() -> None:
    assert create_adapter("adk").name == "adk"
    assert create_adapter("autogen").name == "autogen"


# ── M12.17 image gen ──────────────────────────────────────────────────


def test_image_gen_unavailable() -> None:
    tool = ImageGenTool()
    assert tool.available is False
    import asyncio

    res = asyncio.run(tool.execute(prompt="x"))
    assert res.success is False
    assert "not configured" in (res.error or "")


def test_image_gen_definition() -> None:
    assert ImageGenTool().definition.name == "generate_image"
