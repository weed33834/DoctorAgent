"""Data governance catalog store + service (M20).

SQLite-backed catalog of data assets with metadata, lineage, quality checks
and sensitivity classification. A real, self-contained implementation for the
"data asset management" capabilities: catalog CRUD, lineage graph, quality
scores and automatic PHI / keyword-based classification.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from doctoragent.governance.models import (
    AssetType,
    ClassificationRule,
    DataAsset,
    DataSensitivity,
    LineageEdge,
    QualityCheck,
)
from doctoragent.model.text_utils import extract_keywords

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class GovernanceStore:
    """SQLite store for the data governance catalog."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS gov_assets (
                    id TEXT PRIMARY KEY, org_id TEXT, name TEXT, asset_type TEXT,
                    source TEXT, sensitivity TEXT, owner TEXT, description TEXT,
                    row_count INTEGER, size_bytes INTEGER, version INTEGER,
                    created_at TEXT, updated_at TEXT, metadata TEXT
                );
                CREATE TABLE IF NOT EXISTS gov_lineage (
                    id TEXT PRIMARY KEY, upstream TEXT, downstream TEXT,
                    transform TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS gov_quality (
                    id TEXT PRIMARY KEY, asset_id TEXT, check_type TEXT,
                    score REAL, status TEXT, detail TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS gov_classification_rules (
                    id TEXT PRIMARY KEY, name TEXT, sensitivity TEXT,
                    keywords TEXT, enabled INTEGER, created_at TEXT
                );
                """
            )
            conn.commit()

    # ── assets ──────────────────────────────────────────────────────

    def upsert_asset(self, a: DataAsset) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gov_assets "
                "(id,org_id,name,asset_type,source,sensitivity,owner,description,"
                "row_count,size_bytes,version,created_at,updated_at,metadata) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.org_id, a.name, a.asset_type.value, a.source,
                 a.sensitivity.value, a.owner, a.description, a.row_count,
                 a.size_bytes, a.version, a.created_at, a.updated_at,
                 json.dumps(a.metadata)),
            )
            conn.commit()

    def get_asset(self, asset_id: str) -> DataAsset | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM gov_assets WHERE id=?", (asset_id,)).fetchone()
        return self._row_asset(row) if row else None

    def list_assets(self, org_id: str | None = None, asset_type: str | None = None,
                    sensitivity: str | None = None) -> list[DataAsset]:
        sql = "SELECT * FROM gov_assets WHERE 1=1"
        params: list[Any] = []
        if org_id:
            sql += " AND org_id=?"; params.append(org_id)
        if asset_type:
            sql += " AND asset_type=?"; params.append(asset_type)
        if sensitivity:
            sql += " AND sensitivity=?"; params.append(sensitivity)
        sql += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_asset(r) for r in rows]

    def delete_asset(self, asset_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM gov_assets WHERE id=?", (asset_id,))
            conn.execute("DELETE FROM gov_lineage WHERE upstream=? OR downstream=?",
                         (asset_id, asset_id))
            conn.execute("DELETE FROM gov_quality WHERE asset_id=?", (asset_id,))
            conn.commit()

    @staticmethod
    def _row_asset(row: Any) -> DataAsset:
        return DataAsset(
            id=row["id"], org_id=row["org_id"], name=row["name"],
            asset_type=AssetType(row["asset_type"]), source=row["source"],
            sensitivity=DataSensitivity(row["sensitivity"]), owner=row["owner"],
            description=row["description"], row_count=row["row_count"],
            size_bytes=row["size_bytes"], version=row["version"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    # ── lineage ─────────────────────────────────────────────────────

    def add_lineage(self, upstream: str, downstream: str, transform: str = "") -> LineageEdge:
        edge = LineageEdge(id=_id("lin"), upstream_asset_id=upstream,
                           downstream_asset_id=downstream, transform=transform, created_at=_now())
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gov_lineage (id,upstream,downstream,transform,created_at) "
                "VALUES (?,?,?,?,?)",
                (edge.id, edge.upstream_asset_id, edge.downstream_asset_id,
                 edge.transform, edge.created_at),
            )
            conn.commit()
        return edge

    def get_lineage(self, asset_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            upstream = conn.execute(
                "SELECT * FROM gov_lineage WHERE downstream=?", (asset_id,)
            ).fetchall()
            downstream = conn.execute(
                "SELECT * FROM gov_lineage WHERE upstream=?", (asset_id,)
            ).fetchall()
        return {
            "upstream": [dict(r) for r in upstream],
            "downstream": [dict(r) for r in downstream],
        }

    # ── quality ─────────────────────────────────────────────────────

    def add_quality(self, q: QualityCheck) -> QualityCheck:
        if q.id == "":
            q.id = _id("q")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gov_quality "
                "(id,asset_id,check_type,score,status,detail,created_at) VALUES (?,?,?,?,?,?,?)",
                (q.id, q.asset_id, q.check_type, q.score, q.status, q.detail, q.created_at or _now()),
            )
            conn.commit()
        return q

    def quality_for(self, asset_id: str) -> list[QualityCheck]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM gov_quality WHERE asset_id=? ORDER BY created_at DESC",
                (asset_id,),
            ).fetchall()
        return [
            QualityCheck(id=r["id"], asset_id=r["asset_id"], check_type=r["check_type"],
                         score=r["score"], status=r["status"], detail=r["detail"],
                         created_at=r["created_at"])
            for r in rows
        ]

    # ── classification rules ────────────────────────────────────────

    def upsert_rule(self, r: ClassificationRule) -> ClassificationRule:
        if r.id == "":
            r.id = _id("rule")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gov_classification_rules "
                "(id,name,sensitivity,keywords,enabled,created_at) VALUES (?,?,?,?,?,?)",
                (r.id, r.name, r.sensitivity.value, json.dumps(r.keywords),
                 1 if r.enabled else 0, r.created_at or _now()),
            )
            conn.commit()
        return r

    def list_rules(self, enabled_only: bool = True) -> list[ClassificationRule]:
        sql = "SELECT * FROM gov_classification_rules"
        if enabled_only:
            sql += " WHERE enabled=1"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [
            ClassificationRule(id=r["id"], name=r["name"],
                               sensitivity=DataSensitivity(r["sensitivity"]),
                               keywords=json.loads(r["keywords"] or "[]"),
                               enabled=bool(r["enabled"]), created_at=r["created_at"])
            for r in rows
        ]


class GovernanceService:
    """Facade over the governance store with classification & quality logic."""

    def __init__(self, store: GovernanceStore) -> None:
        self.store = store

    def register_asset(self, name: str, asset_type: AssetType, *, org_id: str = "default",
                       source: str = "", description: str = "", content: str = "",
                       row_count: int = 0, size_bytes: int = 0) -> DataAsset:
        """Register an asset, auto-classifying its sensitivity from content."""
        sensitivity = self._classify(content)
        now = _now()
        asset = DataAsset(
            id=_id("asset"), org_id=org_id, name=name, asset_type=asset_type,
            source=source, sensitivity=sensitivity, description=description,
            row_count=row_count, size_bytes=size_bytes, version=1,
            created_at=now, updated_at=now,
            metadata={"keywords": extract_keywords(content, limit=8)} if content else {},
        )
        self.store.upsert_asset(asset)
        # Automatic quality check: completeness based on presence of content.
        self.store.add_quality(QualityCheck(
            id="", asset_id=asset.id, check_type="completeness",
            score=0.9 if content else 0.3,
            status="pass" if content else "warn",
            detail="content present" if content else "no content registered",
            created_at=_now(),
        ))
        return asset

    def _classify(self, content: str) -> DataSensitivity:
        rules = self.store.list_rules()
        for rule in rules:
            if any(k in content for k in rule.keywords):
                return rule.sensitivity
        return DataSensitivity.INTERNAL

    def add_classification_rule(self, name: str, sensitivity: DataSensitivity,
                                keywords: list[str]) -> ClassificationRule:
        return self.store.upsert_rule(
            ClassificationRule(id="", name=name, sensitivity=sensitivity,
                               keywords=keywords, enabled=True, created_at=_now())
        )

    def record_lineage(self, upstream: str, downstream: str, transform: str = "") -> LineageEdge:
        return self.store.add_lineage(upstream, downstream, transform)

    def catalog_summary(self, org_id: str | None = None) -> dict[str, Any]:
        assets = self.store.list_assets(org_id)
        by_type: dict[str, int] = {}
        by_sensitivity: dict[str, int] = {}
        total_size = 0
        for a in assets:
            by_type[a.asset_type.value] = by_type.get(a.asset_type.value, 0) + 1
            by_sensitivity[a.sensitivity.value] = by_sensitivity.get(a.sensitivity.value, 0) + 1
            total_size += a.size_bytes
        return {
            "assets": len(assets),
            "by_type": by_type,
            "by_sensitivity": by_sensitivity,
            "total_size_bytes": total_size,
            "phi_assets": by_sensitivity.get("phi", 0),
        }
