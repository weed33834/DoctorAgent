"""
医学知识库版本管理系统。
为临床规则、参考范围、药物相互作用等知识库提供版本追踪，
确保历史决策可追溯到当时使用的规则版本。
满足 FDA SaMD / 21 CFR Part 11 合规要求。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date
from pathlib import Path

from pydantic import BaseModel

__all__ = [
    "KnowledgeVersion",
    "KnowledgeVersionManager",
]


class KnowledgeVersion(BaseModel):
    """医学知识库的单个版本记录。

    一次不可变快照，记录某类知识库在指定版本下的内容哈希与元信息，
    供审计与历史决策回溯使用。
    """

    # 知识库类型，如 "reference_ranges" / "allergy_rules" / "ddi_rules" / "clinical_rules"
    knowledge_type: str
    # 语义化版本号，如 "1.2.0"
    version: str
    # 变更说明
    changelog: str
    # 生效日期（ISO 8601 日期）
    effective_date: str
    # 作者/来源
    author: str
    # 知识库内容的 SHA-256 哈希
    content_hash: str


class KnowledgeVersionManager:
    """医学知识库版本管理器，使用 SQLite 持久化版本历史。

    每类知识库按 ``(knowledge_type, version)`` 唯一存储一个版本记录；
    同一版本重复注册会覆盖旧记录并刷新内容哈希。``get_current_version``
    返回生效日期最新的版本，``get_version_at`` 返回指定日期当时有效的版本，
    便于把历史临床决策回溯到当时所依据的规则版本。
    """

    def __init__(self, storage_path: Path | None = None) -> None:
        # 默认存储到用户主目录下的隐藏目录，避免污染工作区
        if storage_path is None:
            storage_path = Path.home() / ".doctoragent" / "knowledge_versions.db"
        self.storage_path = Path(storage_path)
        # 确保父目录存在
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表结构。"""
        with sqlite3.connect(self.storage_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_versions (
                    knowledge_type TEXT,
                    version TEXT,
                    changelog TEXT,
                    effective_date TEXT,
                    author TEXT,
                    content_hash TEXT,
                    PRIMARY KEY(knowledge_type, version)
                )
                """
            )
            conn.commit()

    @staticmethod
    def _compute_hash(content: str) -> str:
        """计算知识库内容的 SHA-256 哈希。"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _row_to_model(self, row: tuple) -> KnowledgeVersion:
        """将数据库行转换为 :class:`KnowledgeVersion` 模型。"""
        return KnowledgeVersion(
            knowledge_type=row[0],
            version=row[1],
            changelog=row[2],
            effective_date=row[3],
            author=row[4],
            content_hash=row[5],
        )

    def register_version(
        self,
        knowledge_type: str,
        version: str,
        content: str,
        changelog: str = "",
        author: str = "system",
    ) -> KnowledgeVersion:
        """注册新版本，自动计算 content_hash 并持久化。

        生效日期取注册当天的 ISO 日期。若同一 ``(knowledge_type, version)``
        已存在，则覆盖旧记录（便于修正内容哈希）。
        """
        content_hash = self._compute_hash(content)
        effective_date = date.today().isoformat()
        kv = KnowledgeVersion(
            knowledge_type=knowledge_type,
            version=version,
            changelog=changelog,
            effective_date=effective_date,
            author=author,
            content_hash=content_hash,
        )
        with sqlite3.connect(self.storage_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO knowledge_versions
                    (knowledge_type, version, changelog, effective_date, author, content_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    kv.knowledge_type,
                    kv.version,
                    kv.changelog,
                    kv.effective_date,
                    kv.author,
                    kv.content_hash,
                ),
            )
            conn.commit()
        return kv

    def get_current_version(self, knowledge_type: str) -> KnowledgeVersion | None:
        """获取指定知识类型的当前（最新）版本。"""
        with sqlite3.connect(self.storage_path) as conn:
            row = conn.execute(
                """
                SELECT knowledge_type, version, changelog, effective_date, author, content_hash
                FROM knowledge_versions
                WHERE knowledge_type = ?
                ORDER BY effective_date DESC, version DESC
                LIMIT 1
                """,
                (knowledge_type,),
            ).fetchone()
        return self._row_to_model(row) if row else None

    def get_version_at(self, knowledge_type: str, date: str) -> KnowledgeVersion | None:
        """获取指定日期有效的版本（effective_date <= date 的最新版本）。"""
        with sqlite3.connect(self.storage_path) as conn:
            row = conn.execute(
                """
                SELECT knowledge_type, version, changelog, effective_date, author, content_hash
                FROM knowledge_versions
                WHERE knowledge_type = ? AND effective_date <= ?
                ORDER BY effective_date DESC, version DESC
                LIMIT 1
                """,
                (knowledge_type, date),
            ).fetchone()
        return self._row_to_model(row) if row else None

    def list_versions(self, knowledge_type: str) -> list[KnowledgeVersion]:
        """获取指定知识类型的版本历史（按生效日期升序）。"""
        with sqlite3.connect(self.storage_path) as conn:
            rows = conn.execute(
                """
                SELECT knowledge_type, version, changelog, effective_date, author, content_hash
                FROM knowledge_versions
                WHERE knowledge_type = ?
                ORDER BY effective_date ASC, version ASC
                """,
                (knowledge_type,),
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

    def verify_content(self, knowledge_type: str, content: str) -> bool:
        """验证当前内容是否与注册的当前版本内容一致。

        重新计算传入内容的 SHA-256 哈希，与当前版本的 ``content_hash``
        比较；若该类型尚无注册版本则返回 ``False``。
        """
        current = self.get_current_version(knowledge_type)
        if current is None:
            return False
        return self._compute_hash(content) == current.content_hash
