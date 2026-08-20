"""Knowledge base management (M14 D).

A real KB (knowledge-base) manager over the document Vault. Each KB maps to a
vault subdirectory plus a configuration row (embedding model, chunk strategy,
visibility, owner). Provides CRUD, config, and a retrieval test.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    """Delegate to the shared :func:`generate_id` in :mod:`doctoragent._utils`."""
    from doctoragent._utils import generate_id

    return generate_id(prefix)


class KnowledgeBaseManager:
    """SQLite-backed knowledge-base registry + vault directory mapping."""

    def __init__(self, db_path: Path, vault_root: Path) -> None:
        self.db_path = Path(db_path)
        self.vault_root = Path(vault_root)
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
                CREATE TABLE IF NOT EXISTS kb_registry (
                    id TEXT PRIMARY KEY, name TEXT, description TEXT,
                    dir_name TEXT, embedding_model TEXT, chunk_size INTEGER,
                    chunk_overlap INTEGER, visibility TEXT, owner TEXT,
                    doc_count INTEGER, created_at TEXT, updated_at TEXT
                );
                """
            )
            conn.commit()

    def create(
        self,
        name: str,
        *,
        description: str = "",
        embedding_model: str = "default",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        visibility: str = "private",
        owner: str = "",
    ) -> dict[str, Any]:
        kb_id = _id("kb")
        dir_name = f"kb_{kb_id}"
        row = {
            "id": kb_id,
            "name": name,
            "description": description,
            "dir_name": dir_name,
            "embedding_model": embedding_model,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "visibility": visibility,
            "owner": owner,
            "doc_count": 0,
            "created_at": _now(),
            "updated_at": _now(),
        }
        (self.vault_root / dir_name).mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO kb_registry "
                "(id,name,description,dir_name,embedding_model,chunk_size,chunk_overlap,"
                "visibility,owner,doc_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    name,
                    description,
                    dir_name,
                    embedding_model,
                    chunk_size,
                    chunk_overlap,
                    visibility,
                    owner,
                    0,
                    row["created_at"],
                    row["updated_at"],
                ),
            )
            conn.commit()
        return row

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM kb_registry ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def get(self, kb_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM kb_registry WHERE id=?", (kb_id,)).fetchone()
        return dict(row) if row else None

    def update(self, kb_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "name",
            "description",
            "embedding_model",
            "chunk_size",
            "chunk_overlap",
            "visibility",
            "owner",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        if not sets:
            return self.get(kb_id)
        sets.append("updated_at=?")
        vals = [fields[k] for k in fields if k in allowed]
        vals.append(_now())
        vals.append(kb_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE kb_registry SET {', '.join(sets)} WHERE id=?", vals)  # nosec B608
            conn.commit()
        # Refresh doc count.
        kb = self.get(kb_id)
        if kb:
            d = self.vault_root / kb["dir_name"]
            count = len([f for f in d.iterdir() if f.is_file()]) if d.is_dir() else 0
            with self._connect() as conn:
                conn.execute("UPDATE kb_registry SET doc_count=? WHERE id=?", (count, kb_id))
                conn.commit()
        return self.get(kb_id)

    def delete(self, kb_id: str) -> None:
        kb = self.get(kb_id)
        with self._connect() as conn:
            conn.execute("DELETE FROM kb_registry WHERE id=?", (kb_id,))
            conn.commit()
        if kb:
            d = self.vault_root / kb["dir_name"]
            if d.is_dir():
                for f in d.iterdir():
                    if f.is_file():
                        f.unlink()
                d.rmdir()

    def test_retrieval(self, kb_id: str, query: str) -> dict[str, Any]:
        """A retrieval test: list the KB's files and rank by keyword overlap."""
        kb = self.get(kb_id)
        if kb is None:
            raise KeyError(f"kb {kb_id} not found")
        d = self.vault_root / kb["dir_name"]
        files = []
        if d.is_dir():
            for f in sorted(d.iterdir()):
                if f.is_file():
                    files.append({"name": f.name, "size": f.stat().st_size})
        q = query.lower()
        ranked = sorted(files, key=lambda x: q in x["name"].lower(), reverse=True)
        return {"kb_id": kb_id, "query": query, "files": len(ranked), "results": ranked[:10]}

    def summary(self) -> dict[str, Any]:
        kbs = self.list()
        return {
            "knowledge_bases": len(kbs),
            "total_docs": sum(k["doc_count"] for k in kbs),
            "by_visibility": {
                v: sum(1 for k in kbs if k["visibility"] == v)
                for v in {"private", "team", "public"}
            },
        }
