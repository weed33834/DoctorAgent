"""SQLite-backed task context persistence."""

import hashlib
import json
import logging
import re
import sqlite3
import struct
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from doctoragent._utils import cosine_similarity as _cosine_similarity
from doctoragent._utils import cosine_similarity_matrix as _cosine_similarity_matrix
from doctoragent._utils import tokenize_for_fts as _tokenize_for_fts
from doctoragent.api.schemas import ClassificationResult, SearchResult, TaskStatus, TaskSummary
from doctoragent.compat import UTC
from doctoragent.model.embedding import LocalEmbeddingProvider
from doctoragent.orchestration.state_machine import TaskState

logger = logging.getLogger(__name__)

# Pattern matching ASCII control characters (0x00-0x1F and 0x7F).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# Shared SQL for upserting a single vector index row (used by both the
# single and batch embedding paths so the column list stays in sync).
_INSERT_VECTOR_SQL = """
    INSERT OR REPLACE INTO vault_vectors
        (task_id, vault_path, category, summary, vector, vector_blob,
         content_hash, model, created_at, tenant_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class TaskStore:
    """Persist task state and context across crashes."""

    def __init__(
        self,
        db_path: Path,
        tenant_id: str = "default",
        vector_store: Any | None = None,
    ) -> None:
        """初始化任务存储。

        ``tenant_id`` 用于多租户隔离：所有写入自动带上该租户 ID，
        所有查询自动过滤到该租户。默认值 ``'default'`` 保证旧单租户
        调用方行为不变（旧数据全部归属于 'default' 租户）。

        ``vector_store``：可选的外部向量后端
        (:class:`~doctoragent.model.vectorstore.base.VectorStoreBackend`)。
        提供时，`index_content_chunks` 会把新/变更 chunk 的向量**双写**
        进该后端，chunk/任务删除会同步删除对应向量；SQLite 仍是元数据
        与正文的唯一事实源，外部后端只承担 ANN 检索。
        """
        if not tenant_id:
            raise ValueError("tenant_id 不能为空")
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._tenant_id = tenant_id
        self._write_lock = threading.Lock()
        self.vector_store = vector_store
        self._init_db()

    @contextmanager
    def _connect(
        self, row_factory: type[sqlite3.Row] | None = None
    ) -> Generator[sqlite3.Connection, None, None]:
        """Open a SQLite connection and ensure it is closed.

        The std-lib ``sqlite3.connect`` context manager only handles
        transactions; it does *not* close the connection on exit.

        Connection settings (WAL, ``busy_timeout``, ``check_same_thread=False``,
        ``timeout=30``) come from :func:`doctoragent._utils.open_sqlite`. Writes
        are serialised by ``self._write_lock``.
        """
        from doctoragent._utils import open_sqlite

        conn = open_sqlite(self.db_path, row_factory=row_factory)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create tasks table if not exists and migrate schema."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
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
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    parent_task_id TEXT
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "created_at" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN created_at TEXT DEFAULT ''")
            if "updated_at" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN updated_at TEXT DEFAULT ''")
            # 多租户 schema 演进：旧库无 tenant_id 列时补加，幂等。
            if "tenant_id" not in columns:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
                )
            # Phase 8.3 多 Agent 协作：tasks 表加 parent_task_id 列（可空，根任务为 NULL）。
            if "parent_task_id" not in columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN parent_task_id TEXT")
            self._init_fts(conn)
            self._init_vectors(conn)
            self._init_chunks(conn)
            conn.commit()

    def close(self) -> None:
        """Close any persistent resources.

        ``TaskStore`` opens connections per-operation (via ``_connect``),
        so there are no long-lived handles to close.  This method exists
        for API symmetry and to support the context-manager protocol.
        """
        # Nothing to close; connections are opened/closed per operation.

    def __enter__(self) -> "TaskStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _init_fts(self, conn: sqlite3.Connection) -> None:
        """Create the SQLite FTS index for vault metadata if supported.

        多租户 schema 演进：FTS5 虚表自带 tenant_id 列。若检测到旧表无
        tenant_id 列，则备份数据、重建虚表、回填 'default' 租户。fallback
        普通表通过 ALTER TABLE 补列。
        """
        self._fts5_enabled = self._has_fts5(conn)
        if self._fts5_enabled:
            # 检查现有 vault_fts 虚表是否已含 tenant_id 列；若不含则重建。
            needs_rebuild = False
            try:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(vault_fts)").fetchall()}
            except sqlite3.Error:
                cols = set()
            if cols and "tenant_id" not in cols:
                needs_rebuild = True
            if needs_rebuild:
                # 备份旧数据 → DROP 旧表 → 建新表（含 tenant_id）→ 回填 'default'。
                backup_rows = conn.execute(
                    "SELECT task_id, vault_path, category, summary, tags, disguise_name, "
                    "created_at FROM vault_fts"
                ).fetchall()
                conn.execute("DROP TABLE vault_fts")
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE vault_fts USING fts5(
                        task_id UNINDEXED,
                        vault_path UNINDEXED,
                        category,
                        summary,
                        tags,
                        disguise_name,
                        created_at UNINDEXED,
                        tenant_id UNINDEXED
                    )
                    """
                )
                conn.executemany(
                    """
                    INSERT INTO vault_fts
                        (task_id, vault_path, category, summary, tags, disguise_name,
                         created_at, tenant_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'default')
                    """,
                    backup_rows,
                )
            else:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(
                        task_id UNINDEXED,
                        vault_path UNINDEXED,
                        category,
                        summary,
                        tags,
                        disguise_name,
                        created_at UNINDEXED,
                        tenant_id UNINDEXED
                    )
                    """
                )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_fts_fallback (
                    task_id TEXT PRIMARY KEY,
                    vault_path TEXT,
                    category TEXT,
                    summary TEXT,
                    tags TEXT,
                    disguise_name TEXT,
                    created_at TEXT,
                    tenant_id TEXT NOT NULL DEFAULT 'default'
                )
                """
            )
            # 旧 fallback 表无 tenant_id 时补加，幂等。
            fcols = {
                row[1] for row in conn.execute("PRAGMA table_info(vault_fts_fallback)").fetchall()
            }
            if fcols and "tenant_id" not in fcols:
                conn.execute(
                    "ALTER TABLE vault_fts_fallback "
                    "ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
                )

    @staticmethod
    def _has_fts5(conn: sqlite3.Connection) -> bool:
        """Return True when the SQLite build supports FTS5."""
        try:
            rows = conn.execute("PRAGMA compile_options").fetchall()
        except sqlite3.Error:
            return False
        return any(str(row[0]) == "ENABLE_FTS5" for row in rows)

    @staticmethod
    def _init_vectors(conn: sqlite3.Connection) -> None:
        """Create the vector index table for semantic search.

        Vectors are stored as BLOB (struct-packed doubles) with a JSON text
        column kept for backwards-compatible reads on legacy databases.
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vault_vectors (
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
        # Migrate: add vector_blob BLOB and content_hash columns for legacy databases.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(vault_vectors)").fetchall()}
        if "vector_blob" not in columns:
            conn.execute("ALTER TABLE vault_vectors ADD COLUMN vector_blob BLOB")
        if "content_hash" not in columns:
            conn.execute("ALTER TABLE vault_vectors ADD COLUMN content_hash TEXT")
        # 多租户 schema 演进：旧库无 tenant_id 列时补加，幂等。
        if "tenant_id" not in columns:
            conn.execute(
                "ALTER TABLE vault_vectors ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
            )

    def _init_chunks(self, conn: sqlite3.Connection) -> None:
        """Create the vault_chunks table and its FTS index for RAG retrieval.

        Stores text chunks extracted from vault files for hybrid search
        (BM25 + Dense) and re-ranking. The companion ``vault_chunks_fts``
        virtual table powers BM25 keyword search; without it the BM25
        retrieval path silently returns empty results.
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vault_chunks (
                chunk_id TEXT PRIMARY KEY,
                task_id TEXT,
                vault_path TEXT,
                category TEXT,
                summary TEXT,
                chunk_index INTEGER,
                text TEXT,
                start_char INTEGER,
                end_char INTEGER,
                content_hash TEXT,
                vector_blob BLOB,
                embedding BLOB,
                model TEXT,
                created_at TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default'
            )
            """
        )
        # Create indexes for fast lookups
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_task ON vault_chunks(task_id, tenant_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_vault ON vault_chunks(vault_path, tenant_id)"
        )
        # Migrate: add embedding column for legacy databases.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(vault_chunks)").fetchall()}
        if "embedding" not in columns:
            conn.execute("ALTER TABLE vault_chunks ADD COLUMN embedding BLOB")
        # Parent-child chunk association (recursive retrieval): small chunks
        # point at a larger parent chunk so context can be expanded after a
        # precise small-chunk match. Idempotent migration for legacy DBs.
        if "parent_chunk_id" not in columns:
            conn.execute("ALTER TABLE vault_chunks ADD COLUMN parent_chunk_id TEXT")

        # Create the FTS5 virtual table for BM25 chunk search.
        # This mirrors vault_fts but indexes chunk-level text for fine-grained
        # keyword retrieval inside individual documents.
        if getattr(self, "_fts5_enabled", False):
            fts_cols = set()
            try:
                fts_cols = {
                    row[1] for row in conn.execute("PRAGMA table_info(vault_chunks_fts)").fetchall()
                }
            except sqlite3.Error:
                fts_cols = set()
            if not fts_cols:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS vault_chunks_fts USING fts5(
                        chunk_id UNINDEXED,
                        task_id UNINDEXED,
                        vault_path UNINDEXED,
                        category,
                        summary,
                        text,
                        tenant_id UNINDEXED
                    )
                    """
                )

    @staticmethod
    def _now() -> str:
        """Return an ISO-8601 UTC timestamp string."""
        return datetime.now(UTC).isoformat()

    def create(self, task_id: UUID, source_path: Path) -> TaskStatus:
        """Create a new task record."""
        now = self._now()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO tasks
                    (task_id, state, source_path, created_at, updated_at, tenant_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(task_id),
                    TaskState.IDLE.name,
                    str(source_path),
                    now,
                    now,
                    self._tenant_id,
                ),
            )
            conn.commit()
        return TaskStatus(task_id=task_id, state=TaskState.IDLE.name)

    def update_state(self, task_id: UUID, state: TaskState, message: str = "") -> TaskStatus:
        """Update task state."""
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET state = ?, message = ?, updated_at = ? "
                "WHERE task_id = ? AND tenant_id = ?",
                (state.name, message, self._now(), str(task_id), self._tenant_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"update_state: task {task_id} not found")
            conn.commit()
        return TaskStatus(task_id=task_id, state=state.name, message=message)

    def update_classification(
        self,
        task_id: UUID,
        classification: ClassificationResult,
    ) -> None:
        """Store classification result."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET classification = ? WHERE task_id = ? AND tenant_id = ?",
                (classification.model_dump_json(), str(task_id), self._tenant_id),
            )
            conn.commit()

    def update_vault_result(
        self,
        task_id: UUID,
        vault_path: Path,
        salt: bytes,
        nonce: bytes,
    ) -> None:
        """Store encryption result."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET vault_path = ?, salt = ?, nonce = ? "
                "WHERE task_id = ? AND tenant_id = ?",
                (str(vault_path), salt, nonce, str(task_id), self._tenant_id),
            )
            conn.commit()

    def delete(self, task_id: UUID) -> None:
        """Delete a task and its search index entries."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM tasks WHERE task_id = ? AND tenant_id = ?",
                (str(task_id), self._tenant_id),
            )
            if self._fts5_enabled:
                conn.execute(
                    "DELETE FROM vault_fts WHERE task_id = ? AND tenant_id = ?",
                    (str(task_id), self._tenant_id),
                )
            else:
                conn.execute(
                    "DELETE FROM vault_fts_fallback WHERE task_id = ? AND tenant_id = ?",
                    (str(task_id), self._tenant_id),
                )
            conn.commit()

    def index_classification(
        self,
        task_id: UUID,
        classification: ClassificationResult,
        vault_path: Path,
        created_at: str | None = None,
    ) -> None:
        """Index classification metadata for full-text search."""
        now = created_at or self._now()
        tags = " ".join(classification.tags)
        with self._write_lock, self._connect() as conn:
            if self._fts5_enabled:
                conn.execute(
                    "DELETE FROM vault_fts WHERE task_id = ? AND tenant_id = ?",
                    (str(task_id), self._tenant_id),
                )
                conn.execute(
                    """
                    INSERT INTO vault_fts
                        (task_id, vault_path, category, summary, tags, disguise_name,
                         created_at, tenant_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(task_id),
                        str(vault_path),
                        classification.category,
                        _tokenize_for_fts(classification.summary),
                        tags,
                        classification.disguise_name,
                        now,
                        self._tenant_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO vault_fts_fallback
                        (task_id, vault_path, category, summary, tags, disguise_name,
                         created_at, tenant_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(task_id),
                        str(vault_path),
                        classification.category,
                        classification.summary,
                        tags,
                        classification.disguise_name,
                        now,
                        self._tenant_id,
                    ),
                )
            conn.commit()

    @staticmethod
    def _embedding_text(classification: ClassificationResult) -> str:
        """Build the text representation used for embedding generation."""
        text = f"{classification.summary} {' '.join(classification.tags)}".strip()
        if not text:
            text = classification.category
        return text

    @staticmethod
    def _content_hash_for(classification: ClassificationResult) -> str:
        """Return a stable content hash to detect classification changes."""
        text = TaskStore._embedding_text(classification)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _upsert_vector(
        conn: sqlite3.Connection,
        *,
        task_id: str,
        vault_path: str,
        category: str,
        summary: str,
        vector_json: str,
        vector_blob: bytes,
        content_hash: str,
        model: str,
        created_at: str,
        tenant_id: str,
    ) -> None:
        """Upsert a single row into the vault_vectors table."""
        conn.execute(
            _INSERT_VECTOR_SQL,
            (
                task_id,
                vault_path,
                category,
                summary,
                vector_json,
                vector_blob,
                content_hash,
                model,
                created_at,
                tenant_id,
            ),
        )

    def index_embedding(
        self,
        task_id: UUID,
        vault_path: Path,
        classification: ClassificationResult,
        provider: LocalEmbeddingProvider,
        created_at: str | None = None,
    ) -> None:
        """Generate and store an embedding for summary/tags metadata.

        The vector is stored as a struct-packed BLOB for compact storage.
        A JSON column is kept for backwards-compatible reads on legacy databases.
        Incremental update: re-embeds only when the content hash has changed.
        """
        text = self._embedding_text(classification)
        if not text:
            return
        content_hash = self._content_hash_for(classification)

        # Check if re-embedding is necessary (incremental update).
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content_hash FROM vault_vectors WHERE task_id = ? AND tenant_id = ?",
                (str(task_id), self._tenant_id),
            ).fetchone()
        if row is not None and row[0] == content_hash:
            logger.debug("index_embedding: content unchanged for %s, skipping re-embed", task_id)
            return

        vector = provider.embed([text])[0]
        vector_blob = struct.pack(f"{len(vector)}d", *vector)
        model_name = getattr(provider, "model_name", "unknown")
        now = created_at or self._now()
        with self._write_lock, self._connect() as conn:
            self._upsert_vector(
                conn,
                task_id=str(task_id),
                vault_path=str(vault_path),
                category=classification.category,
                summary=classification.summary,
                vector_json=json.dumps(vector),
                vector_blob=vector_blob,
                content_hash=content_hash,
                model=model_name,
                created_at=now,
                tenant_id=self._tenant_id,
            )
            conn.commit()

    def _push_vector_records(self, records: list[dict[str, Any]]) -> None:
        """Dual-write chunk vectors into the external store (best-effort).

        SQLite stays the source of truth; a failed external write is logged
        and never fails ingestion.
        """
        if self.vector_store is None or not records:
            return
        try:
            self.vector_store.add(records)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "External vector store add() failed for %d record(s): %s",
                len(records),
                exc,
            )

    def _delete_external_vectors(self, chunk_ids: list[str]) -> None:
        """Best-effort removal of chunk vectors from the external store."""
        if self.vector_store is None or not chunk_ids:
            return
        try:
            self.vector_store.delete(chunk_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "External vector store delete() failed for %d id(s): %s",
                len(chunk_ids),
                exc,
            )

    @staticmethod
    def _vector_record(
        chunk_id: str,
        task_id: UUID,
        vault_path: Path,
        category: str,
        text: str,
        embedding: list[float],
        tenant_id: str,
    ) -> dict[str, Any]:
        return {
            "id": chunk_id,
            "vector": embedding,
            "metadata": {
                "tenant_id": tenant_id,
                "task_id": str(task_id),
                "vault_path": str(vault_path),
                "category": category,
            },
            "document": text[:2000],
        }

    def index_content_chunks(
        self,
        task_id: UUID,
        vault_path: Path,
        classification: ClassificationResult,
        chunks: list[dict[str, Any]],
        provider: LocalEmbeddingProvider | None = None,
        created_at: str | None = None,
    ) -> None:
        """Index text chunks extracted from vault content for RAG retrieval.

        Each chunk is stored in the vault_chunks table with its embedding
        for hybrid search (BM25 + Dense).

        **Incremental**: existing chunks whose ``content_hash`` has not
        changed are skipped — no re-embedding, no re-insert. Only new or
        modified chunks are written. When no chunks have changed the method
        returns early after the hash comparison.

        When an external ``vector_store`` is attached, every written chunk's
        embedding is also pushed to it (dual-write).
        """
        now = created_at or self._now()
        vec_records: list[dict[str, Any]] = []

        # Compute content hashes for all incoming chunks.
        incoming: list[tuple[int, str, str, dict[str, Any]]] = []  # (idx, chunk_id, hash, chunk)
        for i, chunk in enumerate(chunks):
            text = chunk.get("text", "")
            if not text:
                continue
            chunk_id = f"{task_id}_{i}"
            chash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            incoming.append((i, chunk_id, chash, chunk))

        if not incoming:
            return

        # Query existing content_hash values for incremental skip.
        existing_hashes: dict[str, str] = {}
        chunk_ids = [item[1] for item in incoming]
        with self._connect(row_factory=sqlite3.Row) as conn:
            placeholders = ",".join("?" * len(chunk_ids))
            rows = conn.execute(
                f"SELECT chunk_id, content_hash FROM vault_chunks "  # nosec B608
                f"WHERE chunk_id IN ({placeholders}) AND tenant_id = ?",
                (*chunk_ids, self._tenant_id),
            ).fetchall()
        for row in rows:
            existing_hashes[row["chunk_id"]] = row["content_hash"] or ""

        # Filter to only changed / new chunks.
        changed: list[tuple[int, str, str, dict[str, Any]]] = []
        for idx, chunk_id, chash, chunk in incoming:
            if existing_hashes.get(chunk_id) != chash:
                changed.append((idx, chunk_id, chash, chunk))

        if not changed:
            logger.debug(
                "index_content_chunks: all %d chunks unchanged for %s, skipping",
                len(incoming),
                task_id,
            )
            return

        # Generate embeddings only for changed chunks.
        changed_texts = [item[3].get("text", "") for item in changed]
        embeddings: list[list[float]] = []
        if provider is not None and changed_texts:
            try:
                embeddings = provider.embed(changed_texts)
            except Exception as e:
                logger.warning("Failed to embed chunks for %s: %s", task_id, e)
                embeddings = []

        with self._write_lock, self._connect() as conn:
            for j, (i, chunk_id, content_hash, chunk) in enumerate(changed):
                text = chunk.get("text", "")

                # Pack embedding
                embedding_blob = None
                if j < len(embeddings) and embeddings[j]:
                    embedding_blob = struct.pack(f"{len(embeddings[j])}d", *embeddings[j])

                conn.execute(
                    """
                    INSERT OR REPLACE INTO vault_chunks
                        (chunk_id, task_id, vault_path, category, summary, chunk_index,
                         text, start_char, end_char, content_hash, embedding, model,
                         created_at, tenant_id, parent_chunk_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        str(task_id),
                        str(vault_path),
                        classification.category,
                        classification.summary,
                        i,
                        text,
                        chunk.get("start_char", 0),
                        chunk.get("end_char", len(text)),
                        content_hash,
                        embedding_blob,
                        getattr(provider, "model_name", "unknown") if provider else "none",
                        now,
                        self._tenant_id,
                        chunk.get("parent_chunk_id"),
                    ),
                )

                # Also index in FTS for BM25 search. The `text` column is
                # jieba-segmented (space-joined words) so FTS5's unicode61
                # tokenizer indexes Chinese word-by-word; the raw text is
                # retained in vault_chunks (above) for display/generation.
                if self._fts5_enabled:
                    # FTS5 has no unique key on chunk_id: OR REPLACE
                    # would append a ghost row. Remove old row first.
                    try:
                        conn.execute(
                            "DELETE FROM vault_chunks_fts WHERE chunk_id = ?",
                            (chunk_id,),
                        )
                    except sqlite3.Error:
                        pass
                    try:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO vault_chunks_fts
                                (chunk_id, task_id, vault_path, category, summary, text, tenant_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                chunk_id,
                                str(task_id),
                                str(vault_path),
                                classification.category,
                                classification.summary,
                                _tokenize_for_fts(text),
                                self._tenant_id,
                            ),
                        )
                    except sqlite3.Error:
                        pass  # FTS table might not exist yet

                # Dual-write: collect the vector for the external backend.
                if self.vector_store is not None and j < len(embeddings) and embeddings[j]:
                    vec_records.append(
                        self._vector_record(
                            chunk_id,
                            task_id,
                            vault_path,
                            classification.category,
                            text,
                            embeddings[j],
                            self._tenant_id,
                        )
                    )

            conn.commit()

        # Push after commit so the SQLite rows exist before ANN hits arrive.
        self._push_vector_records(vec_records)

    def update_chunk_index(
        self,
        task_id: UUID,
        vault_path: Path,
        classification: ClassificationResult,
        chunks: list[dict[str, Any]],
        provider: LocalEmbeddingProvider | None = None,
        created_at: str | None = None,
    ) -> int:
        """Incrementally update the chunk index for *task_id*.

        Unlike :meth:`index_content_chunks` which only inserts/replaces
        chunks, this method also **deletes** chunks that no longer exist
        in the new ``chunks`` list. This is the right call when a file's
        content has changed and the chunk set may have shrunk or been
        reordered.

        Returns the number of chunks that were actually written (new or
        modified). Unchanged chunks are left untouched.
        """
        now = created_at or self._now()
        deleted_ids: list[str] = []
        vec_records: list[dict[str, Any]] = []

        # Build the set of incoming chunk IDs and hashes.
        incoming_ids: set[str] = set()
        incoming: list[tuple[int, str, str, dict[str, Any]]] = []
        for i, chunk in enumerate(chunks):
            text = chunk.get("text", "")
            if not text:
                continue
            chunk_id = f"{task_id}_{i}"
            chash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            incoming_ids.add(chunk_id)
            incoming.append((i, chunk_id, chash, chunk))

        with self._write_lock, self._connect() as conn:
            # Delete stale chunks (exist in DB but not in incoming set).
            existing_rows = conn.execute(
                "SELECT chunk_id, content_hash FROM vault_chunks "
                "WHERE task_id = ? AND tenant_id = ?",
                (str(task_id), self._tenant_id),
            ).fetchall()
            existing_hashes: dict[str, str] = {}
            stale_ids: list[str] = []
            for row in existing_rows:
                cid = row[0]
                existing_hashes[cid] = row[1] or ""
                if cid not in incoming_ids:
                    stale_ids.append(cid)

            for cid in stale_ids:
                conn.execute(
                    "DELETE FROM vault_chunks WHERE chunk_id = ? AND tenant_id = ?",
                    (cid, self._tenant_id),
                )
                if self._fts5_enabled:
                    try:
                        conn.execute(
                            "DELETE FROM vault_chunks_fts WHERE chunk_id = ? AND tenant_id = ?",
                            (cid, self._tenant_id),
                        )
                    except sqlite3.Error:
                        pass
                deleted_ids.append(cid)

            # Determine which chunks are new or changed.
            changed = [
                (i, cid, chash, chunk)
                for i, cid, chash, chunk in incoming
                if existing_hashes.get(cid) != chash
            ]

            if changed:
                changed_texts = [item[3].get("text", "") for item in changed]
                embeddings: list[list[float]] = []
                if provider is not None and changed_texts:
                    try:
                        embeddings = provider.embed(changed_texts)
                    except Exception as e:
                        logger.warning("Failed to embed chunks for %s: %s", task_id, e)
                        embeddings = []

                for j, (i, chunk_id, content_hash, chunk) in enumerate(changed):
                    text = chunk.get("text", "")
                    embedding_blob = None
                    if j < len(embeddings) and embeddings[j]:
                        embedding_blob = struct.pack(f"{len(embeddings[j])}d", *embeddings[j])
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO vault_chunks
                            (chunk_id, task_id, vault_path, category, summary, chunk_index,
                             text, start_char, end_char, content_hash, embedding, model,
                             created_at, tenant_id, parent_chunk_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk_id,
                            str(task_id),
                            str(vault_path),
                            classification.category,
                            classification.summary,
                            i,
                            text,
                            chunk.get("start_char", 0),
                            chunk.get("end_char", len(text)),
                            content_hash,
                            embedding_blob,
                            getattr(provider, "model_name", "unknown") if provider else "none",
                            now,
                            self._tenant_id,
                            chunk.get("parent_chunk_id"),
                        ),
                    )
                    if self._fts5_enabled:
                        # FTS5 has no unique key on chunk_id: OR REPLACE
                        # would append a ghost row. Remove old row first.
                        try:
                            conn.execute(
                                "DELETE FROM vault_chunks_fts WHERE chunk_id = ?",
                                (chunk_id,),
                            )
                        except sqlite3.Error:
                            pass
                        try:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO vault_chunks_fts
                                    (chunk_id, task_id, vault_path, category, summary, text, tenant_id)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    chunk_id,
                                    str(task_id),
                                    str(vault_path),
                                    classification.category,
                                    classification.summary,
                                    _tokenize_for_fts(text),
                                    self._tenant_id,
                                ),
                            )
                        except sqlite3.Error:
                            pass
                    if self.vector_store is not None and j < len(embeddings) and embeddings[j]:
                        vec_records.append(
                            self._vector_record(
                                chunk_id,
                                task_id,
                                vault_path,
                                classification.category,
                                text,
                                embeddings[j],
                                self._tenant_id,
                            )
                        )

            conn.commit()
        # External store: push new vectors, drop stale ones.
        self._delete_external_vectors(deleted_ids)
        self._push_vector_records(vec_records)
        return len(changed)

    def reindex_all(
        self,
        task_ids: list[UUID],
        chunks_by_task: dict[UUID, tuple[Path, ClassificationResult, list[dict[str, Any]]]],
        provider: LocalEmbeddingProvider | None = None,
    ) -> dict[str, int]:
        """Full rebuild of the chunk index for the given *task_ids*.

        Deletes **all** existing chunks for each task, then re-indexes from
        the provided ``chunks_by_task`` mapping. Use this when the chunking
        strategy or embedding model has changed and every chunk must be
        re-processed.

        Parameters:
            task_ids: Tasks to rebuild.
            chunks_by_task: Maps each task_id to
                ``(vault_path, classification, chunks)``.
            provider: Optional embedding provider for vector generation.

        Returns:
            A dict with ``"rebuilt"`` (number of tasks rebuilt) and
            ``"chunks"`` (total chunks written) counts.
        """
        rebuilt = 0
        total_chunks = 0
        for tid in task_ids:
            entry = chunks_by_task.get(tid)
            if entry is None:
                continue
            vault_path, classification, chunks = entry
            # Wipe existing chunks for this task.
            self.delete_content_chunks(tid)
            # Re-index from scratch.
            self.index_content_chunks(
                task_id=tid,
                vault_path=vault_path,
                classification=classification,
                chunks=chunks,
                provider=provider,
            )
            rebuilt += 1
            total_chunks += len(chunks)
        return {"rebuilt": rebuilt, "chunks": total_chunks}

    def get_content_chunks(self, task_id: UUID) -> list[dict[str, Any]]:
        """Get all content chunks for a task."""
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT chunk_id, task_id, vault_path, category, summary, "
                "chunk_index, text, start_char, end_char FROM vault_chunks "
                "WHERE task_id = ? AND tenant_id = ? ORDER BY chunk_index",
                (str(task_id), self._tenant_id),
            ).fetchall()

        return [
            {
                "chunk_id": row["chunk_id"],
                "task_id": row["task_id"],
                "vault_path": row["vault_path"],
                "category": row["category"],
                "summary": row["summary"],
                "chunk_index": row["chunk_index"],
                "text": row["text"],
                "start_char": row["start_char"],
                "end_char": row["end_char"],
            }
            for row in rows
        ]

    def delete_content_chunks(self, task_id: UUID) -> None:
        """Delete all content chunks for a task."""
        chunk_ids: list[str] = []
        with self._write_lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT chunk_id FROM vault_chunks WHERE task_id = ? AND tenant_id = ?",
                (str(task_id), self._tenant_id),
            ).fetchall()
            chunk_ids = [r[0] for r in rows]
            conn.execute(
                "DELETE FROM vault_chunks WHERE task_id = ? AND tenant_id = ?",
                (str(task_id), self._tenant_id),
            )
            # Also delete from FTS if exists
            try:
                conn.execute(
                    "DELETE FROM vault_chunks_fts WHERE task_id = ? AND tenant_id = ?",
                    (str(task_id), self._tenant_id),
                )
            except sqlite3.Error:
                pass
            conn.commit()
        # Best-effort removal from the external ANN store.
        self._delete_external_vectors(chunk_ids)

    def get_chunk_by_id(self, chunk_id: str) -> dict[str, Any] | None:
        """Fetch a single chunk by its ``chunk_id`` (tenant-scoped).

        Used by the recursive-retrieval parent-child expansion: once a small
        chunk is matched, its ``parent_chunk_id`` is resolved through this
        lookup to load the wider context chunk.
        """
        with self._connect(row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                "SELECT chunk_id, task_id, vault_path, category, summary, "
                "chunk_index, text, start_char, end_char, parent_chunk_id "
                "FROM vault_chunks WHERE chunk_id = ? AND tenant_id = ?",
                (chunk_id, self._tenant_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "chunk_id": row["chunk_id"],
            "task_id": row["task_id"],
            "vault_path": row["vault_path"],
            "category": row["category"],
            "summary": row["summary"],
            "chunk_index": row["chunk_index"],
            "text": row["text"],
            "start_char": row["start_char"],
            "end_char": row["end_char"],
            "parent_chunk_id": row["parent_chunk_id"],
        }

    def get_parent_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        """Resolve the parent chunk of *chunk_id* (one level up).

        Returns ``None`` when the chunk has no ``parent_chunk_id`` or when the
        referenced parent does not exist. This powers the parent-child context
        expansion in :class:`doctoragent.model.rag.HybridRetriever`.
        """
        with self._connect(row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                "SELECT parent_chunk_id FROM vault_chunks WHERE chunk_id = ? AND tenant_id = ?",
                (chunk_id, self._tenant_id),
            ).fetchone()
        if row is None:
            return None
        parent_id = row["parent_chunk_id"]
        if not parent_id:
            return None
        return self.get_chunk_by_id(parent_id)

    def list_doc_summary_vectors(self) -> list[dict[str, Any]]:
        """Return per-document summary vectors for recursive retrieval.

        Each row carries the owning ``task_id`` plus the struct-packed
        ``vector_blob`` so the RAG pipeline can build a document-level
        summary index (retrieve documents first, then drill into their
        chunks). Tenant-scoped.
        """
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT task_id, vault_path, category, summary, vector_blob "
                "FROM vault_vectors WHERE tenant_id = ?",
                (self._tenant_id,),
            ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "vault_path": row["vault_path"],
                "category": row["category"],
                "summary": row["summary"],
                "vector_blob": row["vector_blob"],
            }
            for row in rows
        ]

    def batch_index_embeddings(
        self,
        task_ids: list[UUID],
        provider: LocalEmbeddingProvider,
    ) -> None:
        """Batch-encode multiple task texts via a single provider call.

        Only tasks whose ``classification`` exists in the ``tasks`` table and
        whose content hash differs from the cached one are re-embedded, so this
        method is safe to call repeatedly (incremental update).
        """
        if not task_ids:
            return
        texts: list[str] = []
        valid_ids: list[str] = []
        new_hashes: list[str] = []
        vault_paths: list[str] = []
        categories: list[str] = []
        summaries: list[str] = []

        with self._connect(row_factory=sqlite3.Row) as conn:
            for tid in task_ids:
                row = conn.execute(
                    "SELECT classification, vault_path FROM tasks "
                    "WHERE task_id = ? AND tenant_id = ?",
                    (str(tid), self._tenant_id),
                ).fetchone()
                if row is None or not row["classification"]:
                    continue
                try:
                    cls_data = json.loads(row["classification"])
                except (json.JSONDecodeError, TypeError):
                    logger.debug("batch_index_embeddings: invalid classification for %s", tid)
                    continue

                try:
                    cls = ClassificationResult(
                        sensitivity=cls_data.get("sensitivity", "low"),
                        category=cls_data.get("category", "unknown"),
                        tags=cls_data.get("tags", []),
                        summary=cls_data.get("summary", ""),
                        disguise_name=cls_data.get("disguise_name", "unknown"),
                        disguise_extension=cls_data.get("disguise_extension", "dat"),
                    )
                except Exception:
                    logger.debug("batch_index_embeddings: classification parse error for %s", tid)
                    continue

                text = self._embedding_text(cls)
                if not text:
                    continue
                content_hash = self._content_hash_for(cls)

                # Check existing cache.
                vec_row = conn.execute(
                    "SELECT content_hash FROM vault_vectors WHERE task_id = ? AND tenant_id = ?",
                    (str(tid), self._tenant_id),
                ).fetchone()
                if vec_row is not None and vec_row["content_hash"] == content_hash:
                    continue

                texts.append(text)
                valid_ids.append(str(tid))
                new_hashes.append(content_hash)
                vault_paths.append(row["vault_path"] or "")
                categories.append(cls.category)
                summaries.append(cls.summary)

        if not texts:
            return

        model_name = getattr(provider, "model_name", "unknown")
        now = self._now()
        vectors = provider.embed(texts)
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"batch_index_embeddings: expected {len(texts)} vectors, got {len(vectors)}"
            )

        with self._write_lock, self._connect() as conn:
            for vid, vec, chash, vp, cat, summ in zip(
                valid_ids,
                vectors,
                new_hashes,
                vault_paths,
                categories,
                summaries,
                strict=True,
            ):
                vector_blob = struct.pack(f"{len(vec)}d", *vec)
                self._upsert_vector(
                    conn,
                    task_id=vid,
                    vault_path=vp,
                    category=cat,
                    summary=summ,
                    vector_json=json.dumps(vec),
                    vector_blob=vector_blob,
                    content_hash=chash,
                    model=model_name,
                    created_at=now,
                    tenant_id=self._tenant_id,
                )
            conn.commit()

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """Search indexed vault metadata for the given keywords."""
        if self._fts5_enabled:
            return self._search_fts(query, top_k)
        return self._search_fallback(query, top_k)

    def semantic_search(
        self,
        query: str,
        top_k: int,
        provider: LocalEmbeddingProvider,
    ) -> list[SearchResult]:
        """Search vector index by cosine similarity to the query embedding.

        Reads vectors from ``vector_blob`` BLOB column when available, falling
        back to the legacy JSON ``vector`` column for backwards compatibility.
        """
        query = query.strip()
        if not query:
            return []
        query_vector = provider.embed([query])[0]
        # Batch cosine via a single matrix-vector product instead of a
        # Python loop over every stored vector.
        vectors: list[list[float]] = []
        saved: list[sqlite3.Row] = []
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT vault_path, category, summary, vector, vector_blob "
                "FROM vault_vectors WHERE tenant_id = ?",
                (self._tenant_id,),
            ).fetchall()
        for row in rows:
            vector = self._read_vector(row)
            if vector is None:
                continue
            vectors.append(vector)
            saved.append(row)
        if not vectors:
            return []
        scores = _cosine_similarity_matrix(query_vector, vectors)
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        return [
            SearchResult(
                vault_path=Path(saved[i]["vault_path"]),
                category=saved[i]["category"],
                summary=saved[i]["summary"],
                score=float(scores[i]),
            )
            for i in order
        ]

    @staticmethod
    def _read_vector(row: sqlite3.Row) -> list[float] | None:
        """Read a vector from a DB row, preferring BLOB format with JSON fallback."""
        blob = row["vector_blob"]
        if blob is not None:
            try:
                return list(struct.unpack(f"{len(blob) // 8}d", blob))
            except (struct.error, MemoryError):
                pass
        text = row["vector"]
        if text is not None:
            try:
                result: list[float] = json.loads(text)
                return result
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    def hybrid_search(
        self,
        query: str,
        top_k: int = 10,
        fts_weight: float = 0.3,
        semantic_weight: float = 0.7,
        provider: LocalEmbeddingProvider | None = None,
    ) -> list[SearchResult]:
        """Perform weighted hybrid search combining FTS and semantic vector results.

        FTS full-text results and semantic cosine-similarity results are both
        fetched, normalized to [0, 1], and combined via:
        ``final = fts_weight * norm_fts + semantic_weight * norm_semantic``.
        Results are deduplicated and sorted by descending final score.
        """
        query = query.strip()
        if not query:
            return []

        fts_raw = self.search(query, top_k=top_k * 2)
        semantic_raw: list[SearchResult] = []
        if provider is not None:
            semantic_raw = self.semantic_search(query, top_k=top_k * 2, provider=provider)

        return _hybrid_fuse(fts_raw, semantic_raw, top_k, fts_weight, semantic_weight)

    def find_similar(self, task_id: UUID, top_k: int = 5) -> list[dict[str, Any]]:
        """Return the top_k most similar documents to *task_id* by cosine similarity.

        Returns:
            list of ``{"task_id": str, "score": float, "category": str, "summary": str}``.
        """
        with self._connect(row_factory=sqlite3.Row) as conn:
            target_row = conn.execute(
                "SELECT task_id, vault_path, category, summary, vector, vector_blob "
                "FROM vault_vectors WHERE task_id = ? AND tenant_id = ?",
                (str(task_id), self._tenant_id),
            ).fetchone()
        if target_row is None:
            return []

        target_vector = self._read_vector(target_row)
        if target_vector is None:
            return []

        scored: list[tuple[float, sqlite3.Row]] = []
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT task_id, vault_path, category, summary, vector, vector_blob "
                "FROM vault_vectors WHERE task_id != ? AND tenant_id = ?",
                (str(task_id), self._tenant_id),
            ).fetchall()
        for row in rows:
            vector = self._read_vector(row)
            if vector is None:
                continue
            score = self._cosine_similarity(target_vector, vector)
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "task_id": row["task_id"],
                "score": score,
                "category": row["category"] or "",
                "summary": row["summary"] or "",
            }
            for score, row in scored[:top_k]
        ]

    def fetch_vault_paths(self, task_ids: list[str]) -> dict[str, str]:
        """Batch-query the ``task_id`` -> ``vault_path`` mapping from ``vault_vectors``.

        Results are filtered to the current tenant so callers cannot read
        another tenant's vault paths. Empty/missing ``task_id`` values are
        skipped. Returns an empty dict when ``task_ids`` is empty.
        """
        if not task_ids:
            return {}
        result: dict[str, str] = {}
        batch_size = 500
        with self._connect(row_factory=sqlite3.Row) as conn:
            for i in range(0, len(task_ids), batch_size):
                batch = task_ids[i : i + batch_size]
                placeholders = ",".join("?" * len(batch))
                rows = conn.execute(
                    f"SELECT task_id, vault_path FROM vault_vectors "  # nosec B608
                    f"WHERE task_id IN ({placeholders}) AND tenant_id = ?",
                    (*batch, self._tenant_id),
                ).fetchall()
                for row in rows:
                    tid = str(row["task_id"] or "")
                    vp = str(row["vault_path"] or "")
                    if tid:
                        result[tid] = vp
        return result

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors.

        Thin wrapper around :func:`doctoragent._utils.cosine_similarity` kept as a
        static method so existing call sites (``self._cosine_similarity`` and
        ``TaskStore._cosine_similarity``) continue to work.
        """
        return _cosine_similarity(a, b)

    def _search_fts(self, query: str, top_k: int) -> list[SearchResult]:
        """Execute an FTS5 search with prefix matching on each token."""
        # jieba-segment the query so CJK content matches the segmented index.
        # _tokenize_for_fts already strips control characters.
        tokens = [t for t in _tokenize_for_fts(query).split() if t]
        if not tokens:
            return []
        # Quote each token and enable prefix matching. Use OR (not implicit
        # AND) so a document matching any query term is a hit; BM25 ranking
        # surfaces docs matching more / rarer terms first.
        quote = '"'
        escaped_quote = '""'
        match_expr = " OR ".join(
            f"{quote}{token.replace(quote, escaped_quote)}{quote}*" for token in tokens
        )
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                """
                SELECT vault_path, category, summary, rank
                FROM vault_fts
                WHERE vault_fts MATCH ? AND tenant_id = ?
                ORDER BY rank
                LIMIT ?
                """,
                (match_expr, self._tenant_id, top_k),
            ).fetchall()
        return [
            SearchResult(
                vault_path=Path(row["vault_path"]),
                category=row["category"],
                summary=row["summary"],
                score=1.0 / (1.0 + abs(float(row["rank"]))),
            )
            for row in rows
        ]

    def _search_fallback(self, query: str, top_k: int) -> list[SearchResult]:
        """Fallback LIKE-based search when FTS5 is unavailable."""
        tokens = [token.lower() for token in query.split() if token]
        if not tokens:
            return []
        conditions = " OR ".join(
            "(LOWER(category) LIKE ? OR LOWER(summary) LIKE ? OR LOWER(tags) LIKE ?"
            " OR LOWER(disguise_name) LIKE ?)"
            for _ in tokens
        )
        params: list[str] = []
        for token in tokens:
            like = f"%{token}%"
            params.extend([like, like, like, like])
        # tenant_id 占位符 + limit 占位符。
        params.append(self._tenant_id)
        params.append(str(top_k))
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(  # nosec B608
                f"""
                SELECT vault_path, category, summary
                FROM vault_fts_fallback
                WHERE ({conditions}) AND tenant_id = ?
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            SearchResult(
                vault_path=Path(row["vault_path"]),
                category=row["category"],
                summary=row["summary"],
                score=1.0,
            )
            for row in rows
        ]

    def get(self, task_id: UUID) -> dict[str, Any] | None:
        """Fetch task record as a dictionary."""
        with self._connect(row_factory=sqlite3.Row) as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ? AND tenant_id = ?",
                (str(task_id), self._tenant_id),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    # ------------------------------------------------------------------
    # Phase 8.3 多 Agent 协作：子任务管理
    # ------------------------------------------------------------------

    def create_subtask(
        self,
        parent_task_id: UUID,
        source_path: str,
        subtask_role: str = "",
    ) -> UUID:
        """创建子任务，parent_task_id 关联父任务，状态 PENDING_CHILD。

        ``subtask_role`` 存储 worker 角色标识（如 "worker"），写入 message
        字段；后续 :meth:`update_subtask_status` 调用会覆盖该字段。
        """
        task_id = uuid4()
        now = self._now()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO tasks
                    (task_id, state, source_path, message, created_at, updated_at,
                     tenant_id, parent_task_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(task_id),
                    TaskState.PENDING_CHILD.name,
                    source_path,
                    subtask_role,
                    now,
                    now,
                    self._tenant_id,
                    str(parent_task_id),
                ),
            )
            conn.commit()
        return task_id

    def list_subtasks(self, parent_task_id: UUID) -> list[dict[str, Any]]:
        """列出父任务的所有子任务。"""
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE parent_task_id = ? AND tenant_id = ? "
                "ORDER BY created_at ASC, rowid ASC",
                (str(parent_task_id), self._tenant_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_subtask_status(
        self,
        task_id: UUID,
        state: TaskState,
        result: dict[str, Any] | None = None,
    ) -> None:
        """更新子任务状态，可选写入结果到 message 字段。

        ``result`` 不为 ``None`` 时序列化为 JSON 写入 message；为 ``None``
        时清空 message。
        """
        message = json.dumps(result, ensure_ascii=False) if result is not None else ""
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET state = ?, message = ?, updated_at = ? "
                "WHERE task_id = ? AND tenant_id = ?",
                (state.name, message, self._now(), str(task_id), self._tenant_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"update_subtask_status: task {task_id} not found")
            conn.commit()

    def aggregate_subtask_results(self, parent_task_id: UUID) -> dict[str, Any]:
        """聚合所有子任务结果，返回 {'completed': n, 'failed': n, 'results': [...]}。

        子任务的 message 字段若为合法 JSON 则解析后并入 results；否则以
        ``{"raw": message}`` 形式并入。
        """
        subtasks = self.list_subtasks(parent_task_id)
        completed = 0
        failed = 0
        results: list[dict[str, Any]] = []
        for record in subtasks:
            state = record.get("state", "")
            if state == TaskState.COMPLETED.name:
                completed += 1
            elif state == TaskState.FAILED.name:
                failed += 1
            message = record.get("message") or ""
            entry: dict[str, Any] = {
                "task_id": record.get("task_id"),
                "state": state,
            }
            if message:
                try:
                    parsed = json.loads(message)
                    if isinstance(parsed, dict):
                        entry.update(parsed)
                    else:
                        entry["result"] = parsed
                except (json.JSONDecodeError, TypeError):
                    entry["raw"] = message
            results.append(entry)
        return {"completed": completed, "failed": failed, "results": results}

    def get_root_task(self, task_id: UUID) -> dict[str, Any] | None:
        """追溯 task_id 的根任务（parent_task_id 为空的任务）。

        防御性循环上限避免环状引用导致死循环。
        """
        current = self.get(task_id)
        if current is None:
            return None
        # 最多上溯 64 层，避免异常数据形成环导致死循环。
        for _ in range(64):
            parent_id = current.get("parent_task_id")
            if not parent_id:
                return current
            parent = self.get(UUID(str(parent_id)))
            if parent is None:
                return current
            current = parent
        return current

    def load_incomplete(self) -> list[dict[str, Any]]:
        """Return all tasks not in a terminal state."""
        terminal = {TaskState.COMPLETED.name, TaskState.FAILED.name, TaskState.QUARANTINED.name}
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE state NOT IN ({}) AND tenant_id = ?".format(  # nosec B608
                    ",".join("?" * len(terminal))
                ),
                (*terminal, self._tenant_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def counts_by_state(self) -> dict[str, int]:
        """Return task counts grouped by state."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) FROM tasks WHERE tenant_id = ? GROUP BY state",
                (self._tenant_id,),
            ).fetchall()
        return dict(rows)

    def _active_order_clause(self) -> str:
        """Recency ordering that handles legacy rows without timestamps."""
        return """
            ORDER BY
                CASE WHEN updated_at = '' THEN 0 ELSE 1 END DESC,
                updated_at DESC,
                CASE WHEN created_at = '' THEN 0 ELSE 1 END DESC,
                created_at DESC,
                rowid DESC
        """

    def _rows_to_summaries(self, rows: list[sqlite3.Row]) -> list[TaskSummary]:
        """Convert database rows to TaskSummary objects."""
        summaries: list[TaskSummary] = []
        for row in rows:
            source = row["source_path"]
            summaries.append(
                TaskSummary(
                    task_id=UUID(row["task_id"]),
                    state=row["state"],
                    message=row["message"] or "",
                    source_path=Path(source) if source else None,
                )
            )
        return summaries

    def _fetch_by_states(
        self,
        states: set[str],
        limit: int,
    ) -> list[TaskSummary]:
        """Fetch summaries for tasks whose state is in ``states``."""
        if not states:
            return []
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(  # nosec B608
                f"""
                SELECT task_id, state, message, source_path
                FROM tasks
                WHERE state IN ({",".join("?" * len(states))}) AND tenant_id = ?
                {self._active_order_clause()}
                LIMIT ?
                """,
                (*states, self._tenant_id, limit),
            ).fetchall()
        return self._rows_to_summaries(rows)

    def list_active(self, limit: int = 5) -> list[TaskSummary]:
        """Return non-terminal active tasks ordered by most recent update."""
        active_states = {
            TaskState.IDLE.name,
            TaskState.CLASSIFYING.name,
            TaskState.ENCRYPTING.name,
            TaskState.INDEXING.name,
        }
        return self._fetch_by_states(active_states, limit)

    def list_attention(self, limit: int = 5) -> list[TaskSummary]:
        """Return FAILED/QUARANTINED tasks ordered by most recent update."""
        attention_states = {TaskState.FAILED.name, TaskState.QUARANTINED.name}
        return self._fetch_by_states(attention_states, limit)

    def list_recent(self, limit: int = 10) -> list[TaskSummary]:
        """Return the most recently updated task summaries.

        The result is ordered by recency (most recently updated first) and
        capped at ``limit`` entries. Legacy rows without timestamps are sorted
        to the end by rowid so the UI still receives a stable list.
        """
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(  # nosec B608
                f"""
                SELECT task_id, state, message, source_path
                FROM tasks
                WHERE tenant_id = ?
                {self._active_order_clause()}
                LIMIT ?
                """,
                (self._tenant_id, limit),
            ).fetchall()
        return self._rows_to_summaries(rows)

    def list_vault_files(self, category: str | None = None) -> list[dict[str, Any]]:
        """Return completed vault file metadata, optionally filtered by category."""
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT task_id, vault_path, classification FROM tasks "
                "WHERE vault_path IS NOT NULL AND state = ? AND tenant_id = ? "
                "ORDER BY updated_at DESC, created_at DESC",
                (TaskState.COMPLETED.name, self._tenant_id),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            classification_raw = row["classification"]
            if not classification_raw:
                continue
            try:
                cls_data = json.loads(classification_raw)
            except json.JSONDecodeError:
                continue
            cat = cls_data.get("category", "unknown")
            if category is not None and cat != category:
                continue
            results.append(
                {
                    "task_id": row["task_id"],
                    "vault_path": row["vault_path"],
                    "category": cat,
                    "summary": cls_data.get("summary", ""),
                    "tags": cls_data.get("tags", []),
                }
            )
        return results


# ---------------------------------------------------------------------------
# Module-level helpers for hybrid search fusion, rank fusion, and clustering
# ---------------------------------------------------------------------------


def _normalize_scores(results: list[SearchResult]) -> dict[str, float]:
    """Min-max normalize result scores to [0, 1], keyed by vault_path.

    Returns an empty dict when there are no results.
    """
    if not results:
        return {}
    scores = [r.score for r in results]
    smin, smax = min(scores), max(scores)
    denom = smax - smin
    if denom == 0:
        return {str(r.vault_path): 1.0 for r in results}
    return {str(r.vault_path): (r.score - smin) / denom for r in results}


def _hybrid_fuse(
    fts: list[SearchResult],
    semantic: list[SearchResult],
    top_k: int,
    fts_weight: float,
    semantic_weight: float,
) -> list[SearchResult]:
    """Fuse FTS and semantic results via weighted normalized-score combination.

    Returns the top_k results sorted by descending final score.
    """
    if not fts:
        return semantic[:top_k]
    if not semantic:
        return fts[:top_k]

    fts_norm = _normalize_scores(fts)
    sem_norm = _normalize_scores(semantic)

    merged: dict[str, SearchResult] = {}
    final_scores: dict[str, float] = {}

    for r in fts:
        key = str(r.vault_path)
        merged[key] = r
        s_fts = fts_norm.get(key, 0.0)
        s_sem = sem_norm.get(key, 0.0)
        final_scores[key] = fts_weight * s_fts + semantic_weight * s_sem

    for r in semantic:
        key = str(r.vault_path)
        if key not in merged:
            merged[key] = r
            s_fts = fts_norm.get(key, 0.0)
            s_sem = sem_norm.get(key, 0.0)
            final_scores[key] = fts_weight * s_fts + semantic_weight * s_sem

    combined = [merged[key].model_copy(update={"score": final_scores[key]}) for key in merged]
    combined.sort(key=lambda item: item.score, reverse=True)
    return combined[:top_k]


def rank_fusion(
    results_a: list[SearchResult],
    results_b: list[SearchResult],
    weight_a: float = 0.5,
    weight_b: float = 0.5,
    k: int = 60,
) -> list[SearchResult]:
    """Fuse two ranked result lists using Reciprocal Rank Fusion (RRF).

    Each result is assigned an RRF score of ``1 / (k + rank)`` where *rank*
    is its 0-based position in the list. The final score is the weighted sum
    of RRF scores from both lists. When only one list has results the other
    is treated as empty (its RRF contribution is 0).

    This is useful when merging results from different retrieval pipelines
    whose raw scores are not directly comparable.
    """
    # Edge case: one side is empty.
    if not results_a:
        return results_b
    if not results_b:
        return results_a

    # Compute RRF scores keyed by vault_path.
    rrf_a: dict[str, float] = {}
    for rank, r in enumerate(results_a):
        rrf_a[str(r.vault_path)] = 1.0 / (k + rank)

    rrf_b: dict[str, float] = {}
    for rank, r in enumerate(results_b):
        rrf_b[str(r.vault_path)] = 1.0 / (k + rank)

    # Merge and compute weighted final score.
    merged: dict[str, SearchResult] = {}
    for r in results_a:
        merged[str(r.vault_path)] = r
    for r in results_b:
        key = str(r.vault_path)
        if key not in merged:
            merged[key] = r

    scored: list[SearchResult] = []
    for key, result in merged.items():
        score = weight_a * rrf_a.get(key, 0.0) + weight_b * rrf_b.get(key, 0.0)
        scored.append(result.model_copy(update={"score": score}))

    scored.sort(key=lambda item: item.score, reverse=True)
    return scored
