"""Regression test: update_chunk_index must write TOKENIZED text into FTS.

Pre-fix bug: ``index_content_chunks`` segmented Chinese text via
``_tokenize_for_fts`` before inserting into ``vault_chunks_fts``, but the
incremental-update path (``update_chunk_index``) inserted the RAW text.
FTS5's unicode61 tokenizer treats an entire CJK run as ONE token, so chunks
ingested through the update path were effectively invisible to BM25 search
for any query whose segmented tokens did not equal the full raw sentence.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest

from doctoragent._utils import open_sqlite, tokenize_for_fts as _tok
from doctoragent.api.schemas import ClassificationResult, SensitivityLevel
from doctoragent.model.rag import BM25Search
from doctoragent.orchestration.task_store import TaskStore


def _classification() -> ClassificationResult:
    return ClassificationResult(
        sensitivity=SensitivityLevel.MEDIUM,
        category="clinical",
        summary="s",
        disguise_name="n",
        disguise_extension="md",
    )


@pytest.fixture
def ts(tmp_path: Path) -> TaskStore:
    store = TaskStore(tmp_path / "tasks.db")
    if not store._fts5_enabled:
        pytest.skip("SQLite built without FTS5")
    return store


class TestUpdatePathFtsTokenized:
    def test_updated_chunk_searchable_after_update(self, ts: TaskStore) -> None:
        task_id = uuid.uuid4()
        ts.create(task_id, Path("v.md"))

        ts.index_content_chunks(
            task_id,
            Path("v.md"),
            _classification(),
            [{"text": "阿司匹林肠溶片每日一次"}],
            provider=None,
        )
        ts.update_chunk_index(
            task_id,
            Path("v.md"),
            _classification(),
            [{"text": "华法林抗凝方案调整为利伐沙班"}],
            provider=None,
        )

        hits = BM25Search(ts.db_path, tenant_id="default").search("华法林", top_k=5)
        assert hits, "updated chunk invisible to BM25 — FTS got raw text?"
        assert any("华法林" in h["text"] for h in hits)

    def test_fts_row_stores_tokenized_text(self, ts: TaskStore) -> None:
        task_id = uuid.uuid4()
        ts.create(task_id, Path("v.md"))
        ts.index_content_chunks(
            task_id,
            Path("v.md"),
            _classification(),
            [{"text": "初始文本"}],
            provider=None,
        )
        ts.update_chunk_index(
            task_id,
            Path("v.md"),
            _classification(),
            [{"text": "更新后的临床记录内容"}],
            provider=None,
        )
        with open_sqlite(ts.db_path) as conn:
            row = conn.execute(
                "SELECT f.text FROM vault_chunks_fts f "
                "JOIN vault_chunks c ON c.chunk_id = f.chunk_id "
                "WHERE c.text LIKE '%更新后%'"
            ).fetchone()
        assert row is not None, "updated chunk missing from FTS table"
        assert row[0] == _tok("更新后的临床记录内容")


def _unused_tempfile_guard() -> str:  # pragma: no cover
    return tempfile.mkdtemp(prefix="fts-guard")
