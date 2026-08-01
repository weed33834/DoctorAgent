# mypy: ignore-errors
"""Tests for the pluggable vector store backends.

Covers:
- SQLiteVectorStore add/search/delete/count round-trip
- top_k limiting and empty-store behaviour
- ChromaVectorStore graceful ImportError when chromadb is absent
- create_vector_store factory dispatch and error paths
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from doctoragent.model.vectorstore import (
    VectorRecord,
    VectorSearchResult,
    VectorStoreBackend,
    create_vector_store,
)
from doctoragent.model.vectorstore.sqlite_store import SQLiteVectorStore

# ---------------------------------------------------------------------------
# SQLiteVectorStore
# ---------------------------------------------------------------------------

def _record(
    rid: str, vec: list[float], document: str = "", meta: dict | None = None
) -> VectorRecord:
    return VectorRecord(id=rid, vector=vec, document=document, metadata=meta or {})


def test_sqlite_add_search_delete_count_roundtrip(tmp_path: Path) -> None:
    """add/search/delete/count on SQLiteVectorStore behave as expected."""
    store = SQLiteVectorStore(path=tmp_path / "v.db")
    try:
        assert store.count() == 0

        store.add([
            _record("a", [1.0, 0.0], document="alpha", meta={"cat": "x"}),
            _record("b", [0.0, 1.0], document="beta", meta={"cat": "y"}),
            _record("c", [1.0, 1.0], document="gamma"),
        ])
        assert store.count() == 3

        # Query close to "a" should rank "a" first.
        hits = store.search([1.0, 0.0], top_k=3)
        assert len(hits) == 3
        assert all(isinstance(h, VectorSearchResult) for h in hits)
        assert hits[0].record.id == "a"
        assert hits[0].record.document == "alpha"
        assert hits[0].record.metadata == {"cat": "x"}
        # Cosine similarity of identical direction == ~1.0.
        assert hits[0].score == pytest.approx(1.0, abs=1e-5)
        # Scores descending.
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)

        store.delete(["a", "b"])
        assert store.count() == 1
        # Deleted ids no longer appear in results.
        remaining = [h.record.id for h in store.search([1.0, 0.0], top_k=5)]
        assert remaining == ["c"]
    finally:
        store.close()


def test_sqlite_upsert_by_id(tmp_path: Path) -> None:
    """Re-adding an id replaces the previous record."""
    store = SQLiteVectorStore(path=tmp_path / "v.db")
    try:
        store.add([_record("a", [1.0, 0.0], document="old")])
        store.add([_record("a", [0.0, 1.0], document="new")])
        assert store.count() == 1
        hits = store.search([0.0, 1.0], top_k=1)
        assert hits[0].record.id == "a"
        assert hits[0].record.document == "new"
    finally:
        store.close()


def test_sqlite_dict_input_is_coerced(tmp_path: Path) -> None:
    """Plain dicts are accepted and coerced to VectorRecord."""
    store = SQLiteVectorStore(path=tmp_path / "v.db")
    try:
        store.add([{"id": "1", "vector": [0.1, 0.9], "metadata": {}, "document": "hi"}])
        assert store.count() == 1
    finally:
        store.close()


def test_sqlite_search_top_k_limits_results(tmp_path: Path) -> None:
    """top_k caps the number of returned hits."""
    store = SQLiteVectorStore(path=tmp_path / "v.db")
    try:
        store.add([
            _record("a", [1.0, 0.0]),
            _record("b", [0.9, 0.1]),
            _record("c", [0.8, 0.2]),
            _record("d", [0.0, 1.0]),
        ])
        assert len(store.search([1.0, 0.0], top_k=1)) == 1
        assert len(store.search([1.0, 0.0], top_k=2)) == 2
        assert len(store.search([1.0, 0.0], top_k=10)) == 4
        # top_k <= 0 returns empty.
        assert store.search([1.0, 0.0], top_k=0) == []
        assert store.search([1.0, 0.0], top_k=-3) == []
    finally:
        store.close()


def test_sqlite_search_empty_store_returns_empty(tmp_path: Path) -> None:
    """Searching a store with no records returns [] (not an error)."""
    store = SQLiteVectorStore(path=tmp_path / "v.db")
    try:
        assert store.search([1.0, 2.0, 3.0], top_k=5) == []
        assert store.count() == 0
    finally:
        store.close()


def test_sqlite_context_manager_closes(tmp_path: Path) -> None:
    """The backend works as a context manager and closes on exit."""
    with SQLiteVectorStore(path=tmp_path / "v.db") as store:
        store.add([_record("a", [1.0])])
        assert store.count() == 1
    # After close, count would error; just confirm no exception on close path.
    assert isinstance(store, VectorStoreBackend)


# ---------------------------------------------------------------------------
# ChromaVectorStore graceful degradation
# ---------------------------------------------------------------------------

def test_chroma_raises_importerror_when_chromadb_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When chromadb is not importable, ChromaVectorStore raises ImportError."""
    # Force chromadb to be "not installed" regardless of the host env.
    monkeypatch.setitem(sys.modules, "chromadb", None)
    # Also clear any cached submodules so a partial import doesn't satisfy it.
    for key in list(sys.modules):
        if key == "chromadb" or key.startswith("chromadb."):
            monkeypatch.setitem(sys.modules, key, None)

    from doctoragent.model.vectorstore.chroma_store import ChromaVectorStore

    with pytest.raises(ImportError) as excinfo:
        ChromaVectorStore(path=str(tmp_path / "chroma"))
    assert "pip install chromadb" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def test_factory_creates_sqlite_backend(tmp_path: Path) -> None:
    """create_vector_store('sqlite', path=...) returns a SQLiteVectorStore."""
    store = create_vector_store("sqlite", path=str(tmp_path / "v.db"))
    try:
        assert isinstance(store, SQLiteVectorStore)
        assert isinstance(store, VectorStoreBackend)
        store.add([_record("x", [1.0, 0.0])])
        assert store.count() == 1
    finally:
        store.close()


def test_factory_unknown_backend_raises_valueerror() -> None:
    """Unknown backend names raise ValueError."""
    with pytest.raises(ValueError):
        create_vector_store("unknown", path="/tmp/whatever")
    with pytest.raises(ValueError):
        create_vector_store("pinecone", path="/tmp/whatever")


def test_factory_sqlite_requires_path() -> None:
    """sqlite backend requires a path argument."""
    with pytest.raises(ValueError):
        create_vector_store("sqlite")


def test_factory_chroma_requires_path() -> None:
    """chroma backend requires a path argument (validated before import)."""
    # Even if chromadb were installed, missing path must raise ValueError.
    with pytest.raises(ValueError):
        create_vector_store("chroma")
