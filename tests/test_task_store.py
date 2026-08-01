# mypy: ignore-errors
"""Tests for SQLite-backed task store."""

import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from doctoragent.api.schemas import ClassificationResult, SearchResult
from doctoragent.model.embedding import DeterministicEmbeddingProvider
from doctoragent.orchestration.state_machine import TaskState
from doctoragent.orchestration.task_store import TaskStore, rank_fusion


@pytest.fixture
def task_store(tmp_path: Path) -> TaskStore:
    """Fixture providing an isolated TaskStore."""
    return TaskStore(tmp_path / "tasks.db")


def test_create_task(task_store: TaskStore) -> None:
    """Creating a task stores an IDLE record."""
    task_id = uuid4()
    source = Path("/tmp/inbox/file.txt")

    status = task_store.create(task_id, source)

    assert status.task_id == task_id
    assert status.state == TaskState.IDLE.name
    record = task_store.get(task_id)
    assert record is not None
    assert record["state"] == TaskState.IDLE.name
    assert record["source_path"] == str(source)


def test_update_state(task_store: TaskStore) -> None:
    """update_state persists the new state and message."""
    task_id = uuid4()
    task_store.create(task_id, Path("/tmp/inbox/file.txt"))

    status = task_store.update_state(task_id, TaskState.CLASSIFYING, "working")

    assert status.state == TaskState.CLASSIFYING.name
    assert status.message == "working"
    record = task_store.get(task_id)
    assert record is not None
    assert record["state"] == TaskState.CLASSIFYING.name
    assert record["message"] == "working"


def test_update_classification(task_store: TaskStore) -> None:
    """Classification result is serialized as JSON."""
    task_id = uuid4()
    task_store.create(task_id, Path("/tmp/inbox/file.txt"))
    classification = ClassificationResult(
        sensitivity="medium",
        category="work",
        disguise_name="report",
        disguise_extension="log",
    )

    task_store.update_classification(task_id, classification)

    record = task_store.get(task_id)
    assert record is not None
    assert classification.model_dump_json() in str(record["classification"])


def test_update_vault_result(task_store: TaskStore) -> None:
    """Vault encryption metadata is stored."""
    task_id = uuid4()
    task_store.create(task_id, Path("/tmp/inbox/file.txt"))
    vault_path = Path("/vault/work/report.log")
    salt = b"salt"
    nonce = b"nonce"

    task_store.update_vault_result(task_id, vault_path, salt, nonce)

    record = task_store.get(task_id)
    assert record is not None
    assert record["vault_path"] == str(vault_path)
    assert record["salt"] == salt
    assert record["nonce"] == nonce


def test_get_missing_task(task_store: TaskStore) -> None:
    """get returns None for unknown task IDs."""
    assert task_store.get(uuid4()) is None


def test_load_incomplete(task_store: TaskStore) -> None:
    """load_incomplete returns only non-terminal tasks."""
    completed_id = uuid4()
    failed_id = uuid4()
    active_id = uuid4()

    task_store.create(completed_id, Path("/tmp/a.txt"))
    task_store.update_state(completed_id, TaskState.COMPLETED)
    task_store.create(failed_id, Path("/tmp/b.txt"))
    task_store.update_state(failed_id, TaskState.FAILED)
    task_store.create(active_id, Path("/tmp/c.txt"))
    task_store.update_state(active_id, TaskState.CLASSIFYING)

    incomplete = task_store.load_incomplete()
    ids = {UUID(r["task_id"]) for r in incomplete}

    assert completed_id not in ids
    assert failed_id not in ids
    assert active_id in ids


def test_counts_by_state(task_store: TaskStore) -> None:
    """counts_by_state groups tasks by their current state."""
    task_store.create(uuid4(), Path("/tmp/a.txt"))
    task_store.create(uuid4(), Path("/tmp/b.txt"))

    counts = task_store.counts_by_state()

    assert counts.get(TaskState.IDLE.name, 0) == 2


def test_list_active_orders_by_recency(task_store: TaskStore) -> None:
    """list_active returns the most recently updated active tasks."""
    first = uuid4()
    second = uuid4()
    task_store.create(first, Path("/tmp/a.txt"))
    task_store.create(second, Path("/tmp/b.txt"))
    task_store.update_state(second, TaskState.CLASSIFYING)

    active = task_store.list_active(limit=5)

    assert len(active) == 2
    assert active[0].task_id == second


def test_list_attention_includes_failed_and_quarantined(
    task_store: TaskStore,
) -> None:
    """list_attention returns FAILED and QUARANTINED tasks."""
    failed = uuid4()
    quarantined = uuid4()
    task_store.create(failed, Path("/tmp/a.txt"))
    task_store.update_state(failed, TaskState.FAILED, "boom")
    task_store.create(quarantined, Path("/tmp/b.txt"))
    task_store.update_state(quarantined, TaskState.QUARANTINED, "suspicious")

    attention = task_store.list_attention(limit=5)
    states = {t.state for t in attention}

    assert states == {TaskState.FAILED.name, TaskState.QUARANTINED.name}


def test_list_attention_respects_limit(task_store: TaskStore) -> None:
    """list_attention honors the limit parameter."""
    for _ in range(5):
        task_id = uuid4()
        task_store.create(task_id, Path("/tmp/a.txt"))
        task_store.update_state(task_id, TaskState.FAILED)

    attention = task_store.list_attention(limit=2)

    assert len(attention) == 2


def test_list_recent_includes_all_states(task_store: TaskStore) -> None:
    """list_recent returns the most recently updated tasks regardless of state."""
    first = uuid4()
    second = uuid4()
    task_store.create(first, Path("/tmp/a.txt"))
    task_store.create(second, Path("/tmp/b.txt"))
    task_store.update_state(first, TaskState.COMPLETED)

    recent = task_store.list_recent(limit=5)

    assert len(recent) == 2
    assert recent[0].task_id == first


def test_fetch_by_states_empty_returns_empty_list(task_store: TaskStore) -> None:
    """_fetch_by_states returns an empty list when no states are requested."""
    assert task_store._fetch_by_states(set(), 10) == []


def test_init_migrates_legacy_schema(tmp_path: Path) -> None:
    """TaskStore adds created_at/updated_at columns to legacy tables."""
    db_path = tmp_path / "legacy.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            source_path TEXT,
            classification TEXT,
            vault_path TEXT,
            salt BLOB,
            nonce BLOB,
            message TEXT DEFAULT ''
        )
        """
    )
    conn.close()

    TaskStore(db_path)
    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    finally:
        conn.close()
    assert "created_at" in columns
    assert "updated_at" in columns


@pytest.fixture
def classification() -> ClassificationResult:
    """Sample classification result for index tests."""
    return ClassificationResult(
        sensitivity="medium",
        category="work",
        tags=["report", "finance"],
        summary="A quarterly finance report",
        disguise_name="team_building_2023",
        disguise_extension="log",
    )


def test_index_classification_and_search(
    task_store: TaskStore, classification: ClassificationResult
) -> None:
    """Indexed classifications can be searched by keywords."""
    task_id = uuid4()
    vault_path = Path("/vault/work/report.log")

    task_store.index_classification(task_id, classification, vault_path)

    results = task_store.search("finance")
    assert len(results) >= 1
    assert results[0].vault_path == vault_path
    assert results[0].category == "work"
    assert "report" in results[0].summary


def test_search_returns_empty_for_no_match(
    task_store: TaskStore, classification: ClassificationResult
) -> None:
    """Search returns an empty list when nothing matches."""
    task_id = uuid4()
    task_store.index_classification(task_id, classification, Path("/vault/work/report.log"))

    assert task_store.search("nonexistent") == []


def test_search_limits_results(task_store: TaskStore, classification: ClassificationResult) -> None:
    """Search respects the top_k limit."""
    for i in range(5):
        task_id = uuid4()
        task_store.index_classification(
            task_id,
            classification.model_copy(update={"summary": f"Report number {i}"}),
            Path(f"/vault/work/report{i}.log"),
        )

    results = task_store.search("Report", top_k=2)
    assert len(results) == 2


def test_search_empty_query_returns_empty(task_store: TaskStore) -> None:
    """An empty query returns no results."""
    assert task_store.search("") == []


def test_fallback_index_and_search(tmp_path: Path, classification: ClassificationResult) -> None:
    """When FTS5 is unavailable the store falls back to a plain table."""
    db_path = tmp_path / "fallback.db"
    store = TaskStore(db_path)
    # Simulate an SQLite build without FTS5.
    store._has_fts5 = lambda _conn: False  # type: ignore[method-assign]
    with store._connect() as conn:
        store._init_fts(conn)

    vault_path = Path("/vault/work/report.log")
    store.index_classification(uuid4(), classification, vault_path)

    results = store.search("finance")
    assert len(results) >= 1
    assert results[0].vault_path == vault_path


def test_has_fts5_handles_database_error(tmp_path: Path) -> None:
    """_has_fts5 returns False when the pragma query fails."""
    db_path = tmp_path / "fts_check.db"
    store = TaskStore(db_path)

    class FailingConnection:
        def execute(self, _sql: str) -> None:
            raise sqlite3.Error("boom")

    assert store._has_fts5(FailingConnection()) is False


def test_fallback_search_empty_query_returns_empty(tmp_path: Path) -> None:
    """Fallback search returns empty for an empty query."""
    db_path = tmp_path / "fallback_empty.db"
    store = TaskStore(db_path)
    store._has_fts5 = lambda _conn: False  # type: ignore[method-assign]
    with store._connect() as conn:
        store._init_fts(conn)

    assert store.search("") == []


@pytest.fixture
def embedding_provider() -> DeterministicEmbeddingProvider:
    """Deterministic provider for vector tests."""
    return DeterministicEmbeddingProvider(dimension=16)


def test_index_embedding_and_semantic_search(
    task_store: TaskStore,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """Indexed embeddings can be retrieved by semantic search."""
    task_id = uuid4()
    vault_path = Path("/vault/work/report.log")

    task_store.index_embedding(task_id, vault_path, classification, embedding_provider)

    results = task_store.semantic_search("finance report", top_k=5, provider=embedding_provider)
    assert len(results) >= 1
    assert results[0].vault_path == vault_path


def test_semantic_search_empty_query_returns_empty(
    task_store: TaskStore,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """Semantic search with an empty query returns no results."""
    assert task_store.semantic_search("", top_k=5, provider=embedding_provider) == []
    assert task_store.semantic_search("   ", top_k=5, provider=embedding_provider) == []


def test_semantic_search_respects_top_k(
    task_store: TaskStore,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """Semantic search respects the top_k limit."""
    for i in range(5):
        task_store.index_embedding(
            uuid4(),
            Path(f"/vault/work/report{i}.log"),
            classification.model_copy(update={"summary": f"Report number {i}"}),
            embedding_provider,
        )

    results = task_store.semantic_search("Report", top_k=2, provider=embedding_provider)
    assert len(results) == 2


def test_index_embedding_uses_category_when_summary_and_tags_empty(
    task_store: TaskStore,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """index_embedding falls back to category when no summary/tags are present."""
    classification = ClassificationResult(
        sensitivity="low",
        category="health",
        tags=[],
        summary="",
        disguise_name="checkup",
        disguise_extension="log",
    )
    vault_path = Path("/vault/health/checkup.log")

    task_store.index_embedding(uuid4(), vault_path, classification, embedding_provider)
    results = task_store.semantic_search("health", top_k=5, provider=embedding_provider)

    assert len(results) == 1
    assert results[0].vault_path == vault_path


# ---------------------------------------------------------------------------
# Hybrid search tests
# ---------------------------------------------------------------------------


def test_hybrid_search_combines_fts_and_semantic(
    task_store: TaskStore,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """hybrid_search returns merged and weighted results from FTS + semantic."""
    vault_path = Path("/vault/work/report.log")
    task_store.index_classification(uuid4(), classification, vault_path)
    task_store.index_embedding(uuid4(), vault_path, classification, embedding_provider)

    results = task_store.hybrid_search("finance report", top_k=5, provider=embedding_provider)
    assert len(results) >= 1
    assert any(r.vault_path == vault_path for r in results)
    # Scores should be in [0, 1] range.
    for r in results:
        assert 0.0 <= r.score <= 1.0


def test_hybrid_search_respects_top_k(
    task_store: TaskStore,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """hybrid_search limits results to top_k."""
    for i in range(5):
        task_id = uuid4()
        vp = Path(f"/vault/work/report{i}.log")
        task_store.index_classification(
            task_id, classification.model_copy(update={"summary": f"Report {i}"}), vp
        )
        task_store.index_embedding(
            task_id,
            vp,
            classification.model_copy(update={"summary": f"Report {i}"}),
            embedding_provider,
        )

    results = task_store.hybrid_search("Report", top_k=3, provider=embedding_provider)
    assert len(results) == 3


def test_hybrid_search_empty_query_returns_empty(
    task_store: TaskStore,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """Empty query returns empty list."""
    assert task_store.hybrid_search("", top_k=5, provider=embedding_provider) == []
    assert task_store.hybrid_search("   ", top_k=5, provider=embedding_provider) == []


def test_hybrid_search_fts_only(
    task_store: TaskStore,
    classification: ClassificationResult,
) -> None:
    """hybrid_search falls back to pure FTS when no provider is given."""
    vault_path = Path("/vault/work/report.log")
    task_store.index_classification(uuid4(), classification, vault_path)

    results = task_store.hybrid_search("finance", top_k=5, provider=None)
    assert len(results) >= 1
    assert results[0].vault_path == vault_path


def test_hybrid_search_custom_weights(
    task_store: TaskStore,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """hybrid_search accepts configurable fts/semantic weights."""
    vault_path = Path("/vault/work/report.log")
    task_store.index_classification(uuid4(), classification, vault_path)
    task_store.index_embedding(uuid4(), vault_path, classification, embedding_provider)

    # FTS-heavy blending.
    results_fts = task_store.hybrid_search(
        "finance report",
        top_k=5,
        fts_weight=0.9,
        semantic_weight=0.1,
        provider=embedding_provider,
    )
    assert len(results_fts) >= 1

    # Semantic-heavy blending.
    results_sem = task_store.hybrid_search(
        "finance report",
        top_k=5,
        fts_weight=0.0,
        semantic_weight=1.0,
        provider=embedding_provider,
    )
    assert len(results_sem) >= 1


# ---------------------------------------------------------------------------
# Rank fusion tests
# ---------------------------------------------------------------------------


def test_rank_fusion_combines_two_lists() -> None:
    """rank_fusion uses RRF to merge two non-empty result lists."""

    r1 = [
        SearchResult(
            vault_path=Path("/a"),
            category="work",
            summary="doc a",
            score=0.9,
        ),
        SearchResult(
            vault_path=Path("/b"),
            category="work",
            summary="doc b",
            score=0.5,
        ),
    ]
    r2 = [
        SearchResult(
            vault_path=Path("/c"),
            category="work",
            summary="doc c",
            score=0.8,
        ),
        SearchResult(
            vault_path=Path("/a"),
            category="work",
            summary="doc a",
            score=0.6,
        ),
    ]
    fused = rank_fusion(r1, r2, weight_a=0.5, weight_b=0.5)
    assert len(fused) == 3  # /a, /b, /c deduplicated
    # /a should appear in both, thus higher combined score.
    scores = {str(r.vault_path): r.score for r in fused}
    assert scores[str(Path("/a"))] > scores[str(Path("/b"))]


def test_rank_fusion_one_side_empty() -> None:
    """rank_fusion returns the non-empty side when one is empty."""

    r1 = [SearchResult(vault_path=Path("/x"), category="w", summary="x", score=0.5)]
    assert rank_fusion(r1, []) == r1
    assert rank_fusion([], r1) == r1


def test_rank_fusion_both_empty() -> None:
    """rank_fusion returns empty when both lists are empty."""

    assert rank_fusion([], []) == []


def test_rank_fusion_different_weights() -> None:
    """rank_fusion respects weight_a / weight_b parameters."""

    r1 = [SearchResult(vault_path=Path("/x"), category="w", summary="x", score=1.0)]
    r2 = [SearchResult(vault_path=Path("/y"), category="w", summary="y", score=1.0)]
    fused_equal = rank_fusion(r1, r2, weight_a=0.5, weight_b=0.5)
    assert len(fused_equal) == 2

    # When weight_a=1 and weight_b=0, /x still dominates.
    fused_a = rank_fusion(r1, r2, weight_a=1.0, weight_b=0.0)
    assert fused_a[0].vault_path == Path("/x")


# ---------------------------------------------------------------------------
# Batch indexing tests
# ---------------------------------------------------------------------------


def test_batch_index_embeddings_indexes_multiple_tasks(
    task_store: TaskStore,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """batch_index_embeddings encodes multiple task texts at once."""
    ids = []
    for i in range(3):
        tid = uuid4()
        vp = Path(f"/vault/work/r{i}.log")
        task_store.create(tid, Path(f"/inbox/r{i}.txt"))
        task_store.update_classification(
            tid,
            classification.model_copy(update={"summary": f"Report {i}"}),
        )
        task_store.update_vault_result(tid, vp, b"salt", b"nonce")
        ids.append(tid)

    task_store.batch_index_embeddings(ids, embedding_provider)

    # Verify all three are searchable.
    for i in range(3):
        results = task_store.semantic_search(f"Report {i}", top_k=5, provider=embedding_provider)
        assert len(results) >= 1


def test_batch_index_embeddings_empty_list_noop(
    task_store: TaskStore,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """batch_index_embeddings with empty list is a no-op."""
    task_store.batch_index_embeddings([], embedding_provider)  # should not raise


def test_batch_index_embeddings_skips_unchanged(
    task_store: TaskStore,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """batch_index_embeddings does not re-embed when content hasn't changed."""
    tid = uuid4()
    vp = Path("/vault/work/r.log")
    task_store.create(tid, Path("/inbox/r.txt"))
    task_store.update_classification(tid, classification)
    task_store.update_vault_result(tid, vp, b"salt", b"nonce")

    # First call embeds.
    task_store.batch_index_embeddings([tid], embedding_provider)

    # Second call should be a no-op (content unchanged).
    # Verify it doesn't raise.
    task_store.batch_index_embeddings([tid], embedding_provider)


# ---------------------------------------------------------------------------
# Index embedding BLOB storage tests
# ---------------------------------------------------------------------------


def test_index_embedding_stores_blob(
    task_store: TaskStore,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """index_embedding stores vector as a BLOB column."""
    vault_path = Path("/vault/work/report.log")
    task_store.index_embedding(uuid4(), vault_path, classification, embedding_provider)

    with task_store._connect() as conn:
        row = conn.execute("SELECT vector_blob, content_hash FROM vault_vectors").fetchone()
        assert row is not None
        assert row[0] is not None  # BLOB is populated
        assert isinstance(row[0], bytes)
        assert row[1] is not None  # content_hash is populated


def test_index_embedding_incremental_skip(
    task_store: TaskStore,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """index_embedding skips re-embedding when content hash is unchanged."""
    vault_path = Path("/vault/work/report.log")
    tid = uuid4()
    task_store.index_embedding(tid, vault_path, classification, embedding_provider)

    with task_store._connect() as conn:
        row1 = conn.execute(
            "SELECT vector_blob FROM vault_vectors WHERE task_id = ?", (str(tid),)
        ).fetchone()
    assert row1 is not None

    # Re-index with same classification — should be a no-op.
    task_store.index_embedding(tid, vault_path, classification, embedding_provider)

    with task_store._connect() as conn:
        row2 = conn.execute(
            "SELECT vector_blob FROM vault_vectors WHERE task_id = ?", (str(tid),)
        ).fetchone()
    assert row2 is not None
    assert row1[0] == row2[0]  # BLOB unchanged


# ---------------------------------------------------------------------------
# find_similar tests
# ---------------------------------------------------------------------------


def test_find_similar_returns_top_k(
    task_store: TaskStore,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """find_similar returns the top_k most similar documents."""
    # Index 3 documents.
    target_id = uuid4()
    ids = [target_id]
    for i in range(3):
        tid = target_id if i == 0 else uuid4()
        if i != 0:
            ids.append(tid)
        vp = Path(f"/vault/work/r{i}.log")
        summary = "finance quarterly report" if i == 0 else f"report {i}"
        task_store.index_embedding(
            tid,
            vp,
            classification.model_copy(update={"summary": summary}),
            embedding_provider,
        )

    similar = task_store.find_similar(target_id, top_k=2)
    assert len(similar) == 2
    for item in similar:
        assert "task_id" in item
        assert "score" in item
        assert "category" in item
        assert "summary" in item
        assert item["task_id"] != str(target_id)


def test_find_similar_missing_task_returns_empty(
    task_store: TaskStore,
) -> None:
    """find_similar returns empty when the target task has no embedding."""
    assert task_store.find_similar(uuid4()) == []
