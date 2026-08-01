"""Prompt 模板与配置的版本控制系统。

支持版本快照、历史查询、差异对比、回滚恢复。
所有版本数据持久化到 SQLite，按 ``(item_id, version)`` 复合主键去重，
每次 ``save_version`` 自增版本号，``rollback`` 通过创建新版本实现
（不破坏历史，可审计）。
"""

from __future__ import annotations

import difflib
import json
import logging
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from doctoragent._utils import open_sqlite
from doctoragent.compat import UTC

logger = logging.getLogger(__name__)


class VersionedItem(BaseModel):
    """版本化条目：单个 Prompt 模板或 Config 的某一版本快照。"""

    id: str
    version: int
    content: str
    metadata: dict = Field(default_factory=dict)
    created_at: str
    author: str
    comment: str = ""


class VersionStore:
    """基于 SQLite 的版本存储。

    表结构::

        versions(item_id TEXT, version INTEGER, content TEXT,
                 metadata TEXT, created_at TEXT, author TEXT, comment TEXT,
                 PRIMARY KEY(item_id, version))
    """

    def __init__(self, storage_path: Path) -> None:
        """初始化版本存储，自动创建父目录与表结构。"""
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _connect(
        self, row_factory: type[sqlite3.Row] | None = None
    ) -> Generator[sqlite3.Connection, None, None]:
        """打开并最终关闭一个 SQLite 连接。

        复用 :func:`doctoragent._utils.open_sqlite` 的 WAL / busy_timeout
        配置；写操作通过 ``self._write_lock`` 串行化。
        """
        conn = open_sqlite(self.storage_path, row_factory=row_factory)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """创建 versions 表与索引（幂等）。"""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS versions (
                    item_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    author TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (item_id, version)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_item ON versions(item_id)")
            conn.commit()

    @staticmethod
    def _now() -> str:
        """返回 ISO-8601 UTC 时间戳字符串。"""
        return datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def save_version(
        self,
        item_id: str,
        content: str,
        author: str = "system",
        comment: str = "",
        metadata: dict | None = None,
    ) -> VersionedItem:
        """保存新版本，``version`` 在该 ``item_id`` 下自增。

        首次保存得到 version=1，之后依次递增。返回新建的
        :class:`VersionedItem`。
        """
        metadata = metadata or {}
        now = self._now()
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) FROM versions WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            current_max = row[0] if row and row[0] is not None else 0
            new_version = current_max + 1
            conn.execute(
                """
                INSERT INTO versions
                    (item_id, version, content, metadata, created_at, author, comment)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    new_version,
                    content,
                    json.dumps(metadata, ensure_ascii=False),
                    now,
                    author,
                    comment,
                ),
            )
            conn.commit()
        item = VersionedItem(
            id=item_id,
            version=new_version,
            content=content,
            metadata=metadata,
            created_at=now,
            author=author,
            comment=comment,
        )
        logger.info("Saved version %d for item %r", new_version, item_id)
        return item

    def delete_item(self, item_id: str) -> None:
        """删除某个 item 的所有版本。"""
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM versions WHERE item_id = ?", (item_id,))
            conn.commit()
        logger.info("Deleted all versions for item %r", item_id)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_version(self, item_id: str, version: int | None = None) -> VersionedItem | None:
        """获取指定版本；``version=None`` 表示最新版本。

        不存在时返回 ``None``。
        """
        with self._connect(row_factory=sqlite3.Row) as conn:
            if version is None:
                row = conn.execute(
                    """
                    SELECT item_id, version, content, metadata, created_at,
                           author, comment
                    FROM versions WHERE item_id = ?
                    ORDER BY version DESC LIMIT 1
                    """,
                    (item_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT item_id, version, content, metadata, created_at,
                           author, comment
                    FROM versions WHERE item_id = ? AND version = ?
                    """,
                    (item_id, version),
                ).fetchone()
        if row is None:
            return None
        return self._row_to_item(row)

    def list_versions(self, item_id: str, limit: int = 50) -> list[VersionedItem]:
        """版本历史列表（按版本号倒序，最多 ``limit`` 条）。"""
        with self._connect(row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                """
                SELECT item_id, version, content, metadata, created_at,
                       author, comment
                FROM versions WHERE item_id = ?
                ORDER BY version DESC LIMIT ?
                """,
                (item_id, limit),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    # ------------------------------------------------------------------
    # diff & rollback
    # ------------------------------------------------------------------

    def diff(self, item_id: str, v1: int, v2: int) -> str:
        """返回 ``v1`` 与 ``v2`` 两个版本之间的 unified diff 文本。

        任一版本不存在时抛出 :class:`ValueError`。
        """
        item1 = self.get_version(item_id, v1)
        item2 = self.get_version(item_id, v2)
        if item1 is None:
            raise ValueError(f"版本不存在: {item_id}@{v1}")
        if item2 is None:
            raise ValueError(f"版本不存在: {item_id}@{v2}")
        lines1 = item1.content.splitlines(keepends=True)
        lines2 = item2.content.splitlines(keepends=True)
        diff_iter = difflib.unified_diff(
            lines1,
            lines2,
            fromfile=f"{item_id} v{v1}",
            tofile=f"{item_id} v{v2}",
        )
        return "".join(diff_iter)

    def rollback(self, item_id: str, version: int) -> VersionedItem:
        """回滚到指定版本。

        通过创建新版本（内容为旧版本）实现，不破坏历史，保持可审计。
        返回新创建的 :class:`VersionedItem`。
        """
        target = self.get_version(item_id, version)
        if target is None:
            raise ValueError(f"回滚目标版本不存在: {item_id}@{version}")
        return self.save_version(
            item_id=item_id,
            content=target.content,
            author="system",
            comment=f"回滚到版本 {version}",
            metadata=dict(target.metadata),
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> VersionedItem:
        """将数据库行转换为 :class:`VersionedItem`。"""
        data = dict(row)
        try:
            metadata = json.loads(data.get("metadata") or "{}")
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        return VersionedItem(
            id=data["item_id"],
            version=data["version"],
            content=data["content"],
            metadata=metadata,
            created_at=data["created_at"],
            author=data["author"],
            comment=data.get("comment", "") or "",
        )
