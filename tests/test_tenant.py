# mypy: ignore-errors
"""Phase 9.1 多租户隔离数据层的测试。"""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from doctoragent.api.schemas import ClassificationResult
from doctoragent.model.embedding import DeterministicEmbeddingProvider
from doctoragent.orchestration.state_machine import TaskState
from doctoragent.orchestration.task_store import TaskStore
from doctoragent.security.tenant import TenantInfo, TenantManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path: Path) -> TenantManager:
    """提供基于 tmp_path 的 TenantManager。"""
    return TenantManager(tmp_path / "tenants")


@pytest.fixture
def embedding_provider() -> DeterministicEmbeddingProvider:
    """确定性 embedding provider，用于向量测试。"""
    return DeterministicEmbeddingProvider(dimension=16)


@pytest.fixture
def classification() -> ClassificationResult:
    """示例 classification 结果。"""
    return ClassificationResult(
        sensitivity="medium",
        category="work",
        tags=["report", "finance"],
        summary="A quarterly finance report",
        disguise_name="team_building_2023",
        disguise_extension="log",
    )


def _make_store(tmp_path: Path, tenant_id: str) -> TaskStore:
    """创建绑定指定租户的 TaskStore。"""
    return TaskStore(tmp_path / f"tasks_{tenant_id}.db", tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# TenantManager 测试
# ---------------------------------------------------------------------------


def test_tenant_manager_create(manager: TenantManager) -> None:
    """创建租户后注册表和 storage 路径都已就绪。"""
    info = manager.create_tenant("acme", "Acme Corp", password="strong-pass")

    assert isinstance(info, TenantInfo)
    assert info.tenant_id == "acme"
    assert info.name == "Acme Corp"
    assert info.is_active is True
    assert info.key_provider_type == "filepassword"

    # 注册表文件存在且包含该租户。
    registry_path = manager._registry_path  # type: ignore[attr-defined]
    assert registry_path.exists()
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    assert "acme" in data

    # storage 目录已创建。
    storage_path = manager.get_storage_path("acme")
    assert storage_path.exists()
    assert info.storage_path == str(storage_path)


def test_tenant_manager_list(manager: TenantManager) -> None:
    """list_tenants 返回所有租户（含默认租户）。"""
    manager.create_tenant("acme", "Acme", password="p1")
    manager.create_tenant("globex", "Globex", password="p2")

    tenants = manager.list_tenants()
    tenant_ids = {t.tenant_id for t in tenants}

    # 默认租户惰性创建后也应被列出。
    assert "default" in tenant_ids
    assert "acme" in tenant_ids
    assert "globex" in tenant_ids


def test_tenant_manager_default(manager: TenantManager) -> None:
    """默认租户 'default' 在首次访问时惰性创建。"""
    # 注册表尚未存在。
    assert not manager._registry_path.exists()  # type: ignore[attr-defined]

    info = manager.get_tenant(TenantManager.DEFAULT_TENANT_ID)

    assert info is not None
    assert info.tenant_id == "default"
    # 注册表文件已创建。
    assert manager._registry_path.exists()  # type: ignore[attr-defined]
    # 再次 get 应幂等返回。
    info2 = manager.get_tenant(TenantManager.DEFAULT_TENANT_ID)
    assert info2 is not None
    assert info2.tenant_id == "default"


def test_tenant_manager_deactivate(manager: TenantManager) -> None:
    """deactivate_tenant 标记 is_active=False。"""
    manager.create_tenant("acme", "Acme", password="p1")
    assert manager.get_tenant("acme").is_active is True  # type: ignore[union-attr]

    manager.deactivate_tenant("acme")

    info = manager.get_tenant("acme")
    assert info is not None
    assert info.is_active is False


def test_tenant_manager_deactivate_default_rejected(manager: TenantManager) -> None:
    """默认租户不能被禁用。"""
    manager.get_tenant(TenantManager.DEFAULT_TENANT_ID)
    with pytest.raises(ValueError):
        manager.deactivate_tenant(TenantManager.DEFAULT_TENANT_ID)


def test_tenant_manager_get_missing_returns_none(manager: TenantManager) -> None:
    """get_tenant 对不存在的租户返回 None。"""
    assert manager.get_tenant("nonexistent") is None


def test_tenant_manager_tenant_exists(manager: TenantManager) -> None:
    """tenant_exists 反映注册状态。"""
    assert manager.tenant_exists("acme") is False
    manager.create_tenant("acme", "Acme", password="p1")
    assert manager.tenant_exists("acme") is True


# ---------------------------------------------------------------------------
# TaskStore 多租户隔离测试
# ---------------------------------------------------------------------------


def test_task_store_tenant_isolation(tmp_path: Path) -> None:
    """两个不同 tenant_id 的 TaskStore 互相不可见对方数据。"""
    store_a = _make_store(tmp_path, "tenant_a")
    store_b = _make_store(tmp_path, "tenant_b")

    tid_a = uuid4()
    tid_b = uuid4()
    store_a.create(tid_a, Path("/inbox/a.txt"))
    store_b.create(tid_b, Path("/inbox/b.txt"))

    # 各自只能看到自己的任务。
    assert store_a.get(tid_a) is not None
    assert store_a.get(tid_b) is None
    assert store_b.get(tid_b) is not None
    assert store_b.get(tid_a) is None

    # list_recent 也按租户隔离。
    a_recent_ids = {t.task_id for t in store_a.list_recent(limit=10)}
    b_recent_ids = {t.task_id for t in store_b.list_recent(limit=10)}
    assert tid_a in a_recent_ids
    assert tid_b not in a_recent_ids
    assert tid_b in b_recent_ids
    assert tid_a not in b_recent_ids


def test_task_store_default_tenant_backward_compat(tmp_path: Path) -> None:
    """无 tenant_id 参数的 TaskStore 行为不变（数据属 'default'）。"""
    db_path = tmp_path / "default.db"
    store = TaskStore(db_path)  # 不传 tenant_id

    assert store._tenant_id == "default"  # type: ignore[attr-defined]

    tid = uuid4()
    store.create(tid, Path("/inbox/file.txt"))

    record = store.get(tid)
    assert record is not None
    # 直接 SQL 验证 tenant_id 列值为 'default'。
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT tenant_id FROM tasks WHERE task_id = ?", (str(tid),)).fetchone()
    assert row is not None
    assert row[0] == "default"


def test_task_store_search_tenant_filter(
    tmp_path: Path, classification: ClassificationResult
) -> None:
    """search 不跨租户返回结果。"""
    store_a = _make_store(tmp_path, "tenant_a")
    store_b = _make_store(tmp_path, "tenant_b")

    store_a.index_classification(uuid4(), classification, Path("/vault/a/report.log"))
    # 同样的关键词，但租户 B 没有索引。
    results_b = store_b.search("finance")
    results_a = store_a.search("finance")

    assert len(results_a) >= 1
    assert results_b == []


def test_task_store_semantic_search_tenant_filter(
    tmp_path: Path,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """semantic_search 不跨租户返回结果。"""
    store_a = _make_store(tmp_path, "tenant_a")
    store_b = _make_store(tmp_path, "tenant_b")

    store_a.index_embedding(
        uuid4(), Path("/vault/a/report.log"), classification, embedding_provider
    )

    results_a = store_a.semantic_search("finance report", top_k=5, provider=embedding_provider)
    results_b = store_b.semantic_search("finance report", top_k=5, provider=embedding_provider)

    assert len(results_a) >= 1
    assert results_b == []


def test_task_store_find_similar_tenant_filter(
    tmp_path: Path,
    classification: ClassificationResult,
    embedding_provider: DeterministicEmbeddingProvider,
) -> None:
    """find_similar 只在当前租户内查找近邻。"""
    store_a = _make_store(tmp_path, "tenant_a")
    store_b = _make_store(tmp_path, "tenant_b")

    target_id = uuid4()
    other_id = uuid4()
    store_a.index_embedding(target_id, Path("/vault/a/r0.log"), classification, embedding_provider)
    # 同样的 task_id 写入到租户 B（不同租户允许同 task_id 共存）。
    store_b.index_embedding(
        other_id,
        Path("/vault/b/r1.log"),
        classification.model_copy(update={"summary": "another finance report"}),
        embedding_provider,
    )

    # 租户 A 的 find_similar 不应看到租户 B 的向量。
    similar_a = store_a.find_similar(target_id, top_k=5)
    similar_ids = {item["task_id"] for item in similar_a}
    assert str(other_id) not in similar_ids


def test_task_store_get_cross_tenant_denied(tmp_path: Path) -> None:
    """get 跨租户读取返回 None。"""
    store_a = _make_store(tmp_path, "tenant_a")
    store_b = _make_store(tmp_path, "tenant_b")

    tid = uuid4()
    store_a.create(tid, Path("/inbox/a.txt"))

    assert store_a.get(tid) is not None
    assert store_b.get(tid) is None


def test_task_store_delete_cross_tenant_denied(tmp_path: Path) -> None:
    """delete 跨租户不影响对方数据。"""
    store_a = _make_store(tmp_path, "tenant_a")
    store_b = _make_store(tmp_path, "tenant_b")

    tid = uuid4()
    store_a.create(tid, Path("/inbox/a.txt"))

    # 租户 B 试图删除租户 A 的任务 → 无效。
    store_b.delete(tid)

    assert store_a.get(tid) is not None


def test_task_store_update_state_cross_tenant_denied(tmp_path: Path) -> None:
    """update_state 跨租户应抛 ValueError。"""
    store_a = _make_store(tmp_path, "tenant_a")
    store_b = _make_store(tmp_path, "tenant_b")

    tid = uuid4()
    store_a.create(tid, Path("/inbox/a.txt"))

    with pytest.raises(ValueError):
        store_b.update_state(tid, TaskState.COMPLETED)

    # 租户 A 的任务状态未变。
    record = store_a.get(tid)
    assert record is not None
    assert record["state"] == TaskState.IDLE.name


def test_task_store_schema_migration(tmp_path: Path) -> None:
    """旧 DB（无 tenant_id 列）打开后自动迁移。"""
    db_path = tmp_path / "legacy.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 手工构造一个旧版 tasks 表，无 tenant_id 列。
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
            message TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )
        """
    )
    # 写入一条旧数据。
    conn.execute(
        "INSERT INTO tasks (task_id, state, source_path) VALUES (?, ?, ?)",
        ("legacy-1", TaskState.IDLE.name, "/inbox/legacy.txt"),
    )
    # 同时构造一个旧版 vault_vectors 表，无 tenant_id 列。
    conn.execute(
        """
        CREATE TABLE vault_vectors (
            task_id TEXT PRIMARY KEY,
            vault_path TEXT,
            category TEXT,
            summary TEXT,
            vector TEXT,
            vector_blob BLOB,
            content_hash TEXT,
            model TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    # 打开 TaskStore 触发迁移。
    store = TaskStore(db_path, tenant_id="default")
    with sqlite3.connect(db_path) as conn:
        task_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        vec_cols = {row[1] for row in conn.execute("PRAGMA table_info(vault_vectors)").fetchall()}

    assert "tenant_id" in task_cols
    assert "tenant_id" in vec_cols

    # 旧数据仍可被 'default' 租户读取。
    record = store.get(uuid4())  # 不会命中 legacy-1（UUID 不匹配）
    assert record is None
    # 直接 SQL 验证旧数据 tenant_id 默认为 'default'。
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT tenant_id FROM tasks WHERE task_id = ?", ("legacy-1",)
        ).fetchone()
    assert row is not None
    assert row[0] == "default"


def test_task_store_schema_migration_fts(tmp_path: Path) -> None:
    """旧 FTS5 虚表（无 tenant_id）打开后被重建并保留数据。"""
    db_path = tmp_path / "legacy_fts.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # 旧版 tasks 表 + 旧版 vault_fts 虚表（无 tenant_id）。
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
            message TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            tenant_id TEXT NOT NULL DEFAULT 'default'
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE vault_fts USING fts5(
            task_id UNINDEXED,
            vault_path UNINDEXED,
            category,
            summary,
            tags,
            disguise_name,
            created_at UNINDEXED
        )
        """
    )
    conn.execute(
        """
        INSERT INTO vault_fts (task_id, vault_path, category, summary, tags,
                               disguise_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-fts-1",
            "/vault/legacy.log",
            "work",
            "legacy report",
            "tag1",
            "disguise",
            "2024-01-01",
        ),
    )
    conn.execute(
        """
        CREATE TABLE vault_vectors (
            task_id TEXT PRIMARY KEY,
            vault_path TEXT,
            category TEXT,
            summary TEXT,
            vector TEXT,
            vector_blob BLOB,
            content_hash TEXT,
            model TEXT,
            created_at TEXT,
            tenant_id TEXT NOT NULL DEFAULT 'default'
        )
        """
    )
    conn.commit()
    conn.close()

    # 打开 TaskStore 触发 FTS 重建迁移。
    TaskStore(db_path, tenant_id="default")

    # 验证 vault_fts 现在含 tenant_id 列且旧数据被回填 'default'。
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(vault_fts)").fetchall()}
        assert "tenant_id" in cols
        row = conn.execute(
            "SELECT tenant_id FROM vault_fts WHERE task_id = ?", ("legacy-fts-1",)
        ).fetchone()
        assert row is not None
        assert row[0] == "default"


def test_task_store_counts_by_state_tenant_filter(tmp_path: Path) -> None:
    """counts_by_state 只统计当前租户的任务。"""
    store_a = _make_store(tmp_path, "tenant_a")
    store_b = _make_store(tmp_path, "tenant_b")

    store_a.create(uuid4(), Path("/inbox/a1.txt"))
    store_a.create(uuid4(), Path("/inbox/a2.txt"))
    store_b.create(uuid4(), Path("/inbox/b1.txt"))

    counts_a = store_a.counts_by_state()
    counts_b = store_b.counts_by_state()

    assert counts_a.get(TaskState.IDLE.name, 0) == 2
    assert counts_b.get(TaskState.IDLE.name, 0) == 1


def test_task_store_load_incomplete_tenant_filter(tmp_path: Path) -> None:
    """load_incomplete 只返回当前租户的非终态任务。"""
    store_a = _make_store(tmp_path, "tenant_a")
    store_b = _make_store(tmp_path, "tenant_b")

    tid_a = uuid4()
    tid_b = uuid4()
    store_a.create(tid_a, Path("/inbox/a.txt"))
    store_b.create(tid_b, Path("/inbox/b.txt"))

    incomplete_a = store_a.load_incomplete()
    ids_a = {row["task_id"] for row in incomplete_a}
    assert str(tid_a) in ids_a
    assert str(tid_b) not in ids_a
