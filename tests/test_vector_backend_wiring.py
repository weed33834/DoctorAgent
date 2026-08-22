"""End-to-end tests: external vector backend wiring (v0.3.18).

Covers the full loop without requiring ``chromadb``:

* **Ingestion dual-write** — ``TaskStore.index_content_chunks`` /
  ``update_chunk_index`` push new-or-changed chunk vectors into an external
  :class:`VectorStoreBackend`; stale/deleted chunk ids are removed from it.
  SQLite stays the source of truth; backend failures never break ingestion.
* **Query delegation** — ``HybridRetriever._dense_search`` serves ANN hits
  from the external store but materialises text/metadata from SQLite rows;
  foreign-tenant hits vanish (row-lookup miss); empty or erroring backends
  fall back to the inline path.
* **Factory/config degradation** — unknown backends and missing extras
  resolve to ``None`` instead of crashing startup.
"""

from __future__ import annotations

import math
import uuid
from pathlib import Path
from typing import Any

import pytest

from doctoragent.api.schemas import ClassificationResult, SensitivityLevel
from doctoragent.model.embedding import LocalEmbeddingProvider
from doctoragent.model.rag import HybridRetriever, RagConfig
from doctoragent.model.vectorstore.base import VectorRecord, VectorSearchResult
from doctoragent.model.vectorstore.factory import get_shared_vector_store
from doctoragent.orchestration.task_store import TaskStore

# ---------------------------------------------------------------------------
# Fakes / stubs
# ---------------------------------------------------------------------------


class KeywordEmbedder(LocalEmbeddingProvider):
    """Deterministic 2-dim embedder: 'warfarin' → x-axis, else y-ish."""

    model_name = "stub-embedder"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            low = t.lower()
            if "warfarin" in low:
                out.append([1.0, 0.0])
            elif "aspirin" in low:
                out.append([0.0, 1.0])
            else:
                out.append([0.7071, 0.7071])
        return out


class FakeVectorBackend:
    """In-memory stand-in for a Chroma-like ANN store."""

    def __init__(self) -> None:
        self.records: dict[str, VectorRecord] = {}
        self.add_calls = 0
        self.delete_calls: list[list[str]] = []
        self.fail_add = False
        self.fail_search = False

    def add(self, records: list[VectorRecord | dict[str, Any]]) -> None:
        self.add_calls += 1
        if self.fail_add:
            raise RuntimeError("backend down")
        for r in records:
            rec = r if isinstance(r, VectorRecord) else VectorRecord(**r)
            self.records[rec.id] = rec

    def search(self, query_vector: list[float], top_k: int = 10) -> list[VectorSearchResult]:
        if self.fail_search:
            raise RuntimeError("backend search exploded")
        scored = []
        qn = _norm(query_vector)
        for rid, rec in self.records.items():
            rn = _norm(rec.vector)
            sim = sum(a * b for a, b in zip(query_vector, rec.vector)) / (qn * rn)
            scored.append(VectorSearchResult(record=rec, score=sim))
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:top_k]

    def delete(self, ids: list[str]) -> None:
        self.delete_calls.append(list(ids))
        for rid in ids:
            self.records.pop(rid, None)

    def count(self) -> int:
        return len(self.records)

    def close(self) -> None:
        pass


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v)) or 1.0


def _classification() -> ClassificationResult:
    return ClassificationResult(
        sensitivity=SensitivityLevel.MEDIUM,
        category="clinical",
        summary="anticoagulation notes",
        disguise_name="notes",
        disguise_extension="md",
    )


@pytest.fixture
def embedder() -> KeywordEmbedder:
    return KeywordEmbedder()


# ---------------------------------------------------------------------------
# Ingestion dual-write
# ---------------------------------------------------------------------------


class TestTaskStoreDualWrite:
    def test_index_pushes_vectors_with_tenant_metadata(
        self, tmp_path: Path, embedder: KeywordEmbedder
    ) -> None:
        backend = FakeVectorBackend()
        ts = TaskStore(tmp_path / "tasks.db", tenant_id="hospital_a", vector_store=backend)
        task_id = uuid.uuid4()
        ts.create(task_id, Path("v.md"))
        chunks = [
            {"text": "warfarin dosing guidance"},
            {"text": "aspirin interaction table"},
        ]
        ts.index_content_chunks(task_id, Path("vault/w.md"), _classification(), chunks, provider=embedder)
        assert backend.count() == 2
        rec = backend.records["%s_0" % task_id]
        assert rec.metadata["tenant_id"] == "hospital_a"
        assert rec.metadata["task_id"] == str(task_id)
        assert rec.metadata["category"] == "clinical"
        assert rec.document.startswith("warfarin")

    def test_unchanged_chunks_not_repushed(
        self, tmp_path: Path, embedder: KeywordEmbedder
    ) -> None:
        backend = FakeVectorBackend()
        ts = TaskStore(tmp_path / "tasks.db", vector_store=backend)
        task_id = uuid.uuid4()
        ts.create(task_id, Path("v.md"))
        chunks = [{"text": "warfarin note"}]
        ts.index_content_chunks(task_id, Path("v.md"), _classification(), chunks, provider=embedder)
        adds_after_first = backend.add_calls
        ts.index_content_chunks(task_id, Path("v.md"), _classification(), chunks, provider=embedder)
        assert backend.count() == 1
        # Second run produced zero changed chunks → backend.add not re-invoked.
        assert backend.add_calls == adds_after_first

    def test_update_removes_stale_vectors(
        self, tmp_path: Path, embedder: KeywordEmbedder
    ) -> None:
        backend = FakeVectorBackend()
        ts = TaskStore(tmp_path / "tasks.db", vector_store=backend)
        task_id = uuid.uuid4()
        ts.create(task_id, Path("v.md"))
        original = [
            {"text": "warfarin alpha"},
            {"text": "aspirin beta"},
            {"text": "misc gamma"},
        ]
        ts.index_content_chunks(task_id, Path("v.md"), _classification(), original, provider=embedder)
        shrunk = [
            {"text": "warfarin alpha"},  # unchanged
            {"text": "aspirin beta v2"},  # changed → re-push under same id
        ]
        ts.update_chunk_index(task_id, Path("v.md"), _classification(), shrunk, provider=embedder)
        stale_id = "%s_2" % task_id
        assert all(cid != stale_id for cid in backend.records)
        assert any(stale_id in batch for batch in backend.delete_calls)

    def test_delete_task_purges_backend(
        self, tmp_path: Path, embedder: KeywordEmbedder
    ) -> None:
        backend = FakeVectorBackend()
        ts = TaskStore(tmp_path / "tasks.db", vector_store=backend)
        task_id = uuid.uuid4()
        ts.create(task_id, Path("v.md"))
        ts.index_content_chunks(
            task_id, Path("v.md"), _classification(), [{"text": "warfarin"}], provider=embedder
        )
        ts.delete_content_chunks(task_id)
        assert backend.count() == 0

    def test_backend_failure_does_not_break_ingestion(
        self, tmp_path: Path, embedder: KeywordEmbedder
    ) -> None:
        backend = FakeVectorBackend()
        backend.fail_add = True
        ts = TaskStore(tmp_path / "tasks.db", vector_store=backend)
        task_id = uuid.uuid4()
        ts.create(task_id, Path("v.md"))
        # Must not raise; SQLite rows are written regardless.
        ts.index_content_chunks(
            task_id, Path("v.md"), _classification(), [{"text": "warfarin"}], provider=embedder
        )
        rows = ts.get_content_chunks(task_id)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Query delegation
# ---------------------------------------------------------------------------


def _build_populated(tmp_path: Path, embedder: KeywordEmbedder):
    """Create a TaskStore + fake backend with two ingested chunks."""
    backend = FakeVectorBackend()
    ts = TaskStore(tmp_path / "tasks.db", tenant_id="hospital_a", vector_store=backend)
    task_id = uuid.uuid4()
    ts.create(task_id, Path("v.md"))
    chunks = [
        {"text": "warfarin anticoagulation protocol"},
        {"text": "aspirin antiplatelet protocol"},
    ]
    ts.index_content_chunks(task_id, Path("v.md"), _classification(), chunks, provider=embedder)
    return ts, backend, task_id


class TestHybridRetrieverExternalPath:
    def test_external_hits_materialised_from_sqlite(
        self, tmp_path: Path, embedder: KeywordEmbedder
    ) -> None:
        ts, backend, _tid = _build_populated(tmp_path, embedder)
        r = HybridRetriever(
            ts.db_path, embedding_provider=embedder, tenant_id='hospital_a', config=RagConfig()
        )
        r._external_store = backend
        results = r.retrieve("warfarin dosing")
        assert results, "external path returned nothing"
        top = results[0]
        # Text/metadata come from SQLite rows, not from the backend document.
        assert "warfarin" in top["text"]
        assert top["text"] == "warfarin anticoagulation protocol"
        # retrieve() fuses with RRF, so the final score is fused-rank based.
        assert top["score"] > 0

    def test_foreign_tenant_hits_filtered_out(
        self, tmp_path: Path, embedder: KeywordEmbedder
    ) -> None:
        ts, backend, _tid = _build_populated(tmp_path, embedder)
        # Another tenant's chunk that is a perfect match for the query.
        backend.add(
            [
                {
                    "id": "foreign_chunk",
                    "vector": [1.0, 0.0],
                    "metadata": {"tenant_id": "hospital_b"},
                    "document": "other hospital warfarin secret",
                }
            ]
        )
        r = HybridRetriever(
            ts.db_path, embedding_provider=embedder, tenant_id='hospital_a', config=RagConfig()
        )
        r._external_store = backend
        results = r.retrieve("warfarin dosing")
        ids = [x["chunk_id"] for x in results]
        assert "foreign_chunk" not in ids

    def test_empty_external_falls_back_inline(
        self, tmp_path: Path, embedder: KeywordEmbedder
    ) -> None:
        ts, _backend, _tid = _build_populated(tmp_path, embedder)
        r = HybridRetriever(
            ts.db_path, embedding_provider=embedder, tenant_id='hospital_a', config=RagConfig()
        )
        r._external_store = FakeVectorBackend()  # count == 0
        results = r.retrieve("warfarin dosing")
        assert results
        assert "warfarin" in results[0]["text"]

    def test_backend_error_falls_back_inline(
        self, tmp_path: Path, embedder: KeywordEmbedder
    ) -> None:
        ts, backend, _tid = _build_populated(tmp_path, embedder)
        backend.fail_search = True
        r = HybridRetriever(
            ts.db_path, embedding_provider=embedder, tenant_id='hospital_a', config=RagConfig()
        )
        r._external_store = backend
        results = r.retrieve("warfarin dosing")
        assert results
        assert "warfarin" in results[0]["text"]

    def test_sqlite_backend_stays_default(
        self, tmp_path: Path, embedder: KeywordEmbedder
    ) -> None:
        """Default RagConfig keeps legacy behaviour: no external store."""
        ts, _backend, _tid = _build_populated(tmp_path, embedder)
        r = HybridRetriever(
            ts.db_path, embedding_provider=embedder, tenant_id='hospital_a', config=RagConfig()
        )
        assert r._external_store is None
        results = r.retrieve("warfarin dosing")
        assert results


# ---------------------------------------------------------------------------
# Factory + config degradation
# ---------------------------------------------------------------------------


class TestFactoryDegradation:
    def test_unknown_backend_returns_none(self, tmp_path: Path) -> None:
        assert get_shared_vector_store("does-not-exist", str(tmp_path)) is None

    def test_chroma_missing_extra_returns_none(self, tmp_path: Path) -> None:
        pytest.importorskip  # noqa: B018 — marker only
        try:
            import chromadb  # noqa: F401
            has_chroma = True
        except ImportError:
            has_chroma = False
        result = get_shared_vector_store("chroma", str(tmp_path / "vs"))
        if not has_chroma:
            assert result is None

    def test_shared_instance_cached(self, tmp_path: Path) -> None:
        a = get_shared_vector_store("sqlite", str(tmp_path / "vs.sqlite"))
        b = get_shared_vector_store("sqlite", str(tmp_path / "vs.sqlite"))
        assert a is b
