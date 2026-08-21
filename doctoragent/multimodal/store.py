"""Multimodal asset library (M26).

A real, SQLite-backed library of multimodal assets (text / audio / image /
video) with automatic modality tagging and cross-modal keyword search. Assets
ingested through the extractors (or registered directly) become searchable by
extracted text + metadata, enabling retrieval across modalities.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from doctoragent._utils import open_sqlite
from doctoragent.model.text_utils import extract_keywords

MODALITIES = ("text", "audio", "image", "video", "document")


def _now() -> str:
    from doctoragent._utils import utcnow_iso

    return utcnow_iso()


def _id(prefix: str) -> str:
    """Delegate to the shared :func:`generate_id` in :mod:`doctoragent._utils`."""
    from doctoragent._utils import generate_id

    return generate_id(prefix)


class MultimodalStore:
    """SQLite store for multimodal assets."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite(self.db_path, row_factory=sqlite3.Row)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mm_assets (
                    id TEXT PRIMARY KEY, name TEXT, modality TEXT, uri TEXT,
                    extracted_text TEXT, keywords TEXT, mime TEXT, size_bytes INTEGER,
                    metadata TEXT, created_at TEXT
                );
                """
            )
            conn.commit()

    def add_asset(
        self,
        name: str,
        modality: str,
        *,
        uri: str = "",
        extracted_text: str = "",
        mime: str = "",
        size_bytes: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if modality not in MODALITIES:
            raise ValueError(f"unknown modality {modality}; supported: {list(MODALITIES)}")
        keywords = extract_keywords(extracted_text, limit=10)
        row = {
            "id": _id("mm"),
            "name": name,
            "modality": modality,
            "uri": uri,
            "extracted_text": extracted_text,
            "keywords": keywords,
            "mime": mime,
            "size_bytes": size_bytes,
            "metadata": metadata or {},
            "created_at": _now(),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO mm_assets (id,name,modality,uri,extracted_text,keywords,mime,"
                "size_bytes,metadata,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    name,
                    modality,
                    uri,
                    extracted_text,
                    json.dumps(keywords, ensure_ascii=False),
                    mime,
                    size_bytes,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    row["created_at"],
                ),
            )
            conn.commit()
        return row

    def search(
        self, query: str, modality: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Cross-modal keyword search over extracted text + keywords + metadata."""
        q = query.lower()
        sql = "SELECT * FROM mm_assets"
        params: list[Any] = []
        if modality:
            sql += " WHERE modality=?"
            params.append(modality)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for r in rows:
            text = (r["extracted_text"] or "").lower()
            kws = json.loads(r["keywords"] or "[]")
            meta = json.dumps(r["metadata"] or {}, ensure_ascii=False).lower()
            score = 0.0
            if q in text:
                score += 2.0
            if any(q in (k or "").lower() for k in kws):
                score += 1.5
            if q in meta:
                score += 1.0
            if q in (r["name"] or "").lower():
                score += 1.0
            if score > 0:
                scored.append(
                    (
                        score,
                        dict(r) | {"keywords": kws, "metadata": json.loads(r["metadata"] or "{}")},
                    )
                )
        scored.sort(key=lambda x: x[0], reverse=True)
        return [row for _, row in scored[:limit]]

    def list_assets(self, modality: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM mm_assets"
        params: list[Any] = []
        if modality:
            sql += " WHERE modality=?"
            params.append(modality)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            dict(r)
            | {
                "keywords": json.loads(r["keywords"] or "[]"),
                "metadata": json.loads(r["metadata"] or "{}"),
            }
            for r in rows
        ]

    def summary(self) -> dict[str, Any]:
        assets = self.list_assets(limit=100000)
        by_modality: dict[str, int] = {}
        total_bytes = 0
        for a in assets:
            by_modality[a["modality"]] = by_modality.get(a["modality"], 0) + 1
            total_bytes += a["size_bytes"]
        return {
            "assets": len(assets),
            "by_modality": by_modality,
            "total_bytes": total_bytes,
        }


class MultimodalService:
    """Facade over the multimodal store, with ingestion from the extractors."""

    def __init__(self, store: MultimodalStore, extractor_manager: Any | None = None) -> None:
        self.store = store
        self.extractor_manager = extractor_manager

    def ingest(
        self,
        name: str,
        modality: str,
        *,
        path: str = "",
        mime: str = "",
        extracted_text: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ingest a file, extracting text via the extractor manager when possible."""
        if not extracted_text and self.extractor_manager is not None and path:
            try:
                result = self.extractor_manager.extract(path)
                extracted_text = getattr(result, "text", "") or str(result)
            except Exception:  # noqa: BLE001 — fall back to no text
                pass
        return self.store.add_asset(
            name,
            modality,
            uri=path,
            extracted_text=extracted_text,
            mime=mime,
            size_bytes=_file_size(path),
            metadata=metadata,
        )

    def search(self, query: str, modality: str | None = None, limit: int = 20) -> dict[str, Any]:
        hits = self.store.search(query, modality=modality, limit=limit)
        return {"query": query, "hits": hits, "total": len(hits)}


def _file_size(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except (OSError, TypeError):
        return 0
