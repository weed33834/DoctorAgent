"""Knowledge Graph RAG module for DoctorAgent.

Builds and queries a knowledge graph over the vault corpus. Entities and
relations are extracted from document chunks by an LLM, persisted in
SQLite, and used to augment retrieval with structured, multi-hop
connections that pure vector / keyword search cannot surface.

Typical flow::

    graph = KnowledgeGraph(db_path)
    graph.build_from_documents(task_store, llm_provider)
    chunks = graph.retrieve("How is Alice connected to Project X?", llm_provider)

Design notes:

* The graph is the canonical store and is persisted to SQLite using the
  same ``open_sqlite`` pattern as :class:`doctoragent.orchestration.task_store.TaskStore`.
* An in-memory mirror (``_entities`` / ``_relations``) is kept in sync so
  that graph traversal (``get_subgraph``) stays cheap.
* LLM responses are parsed with the ``_extract_json`` helper from
  :mod:`doctoragent.model.agent`, tolerating fenced code blocks and prose.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any

from doctoragent._utils import open_sqlite
from doctoragent.compat import UTC
from doctoragent.model.agent import _extract_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_response_text(response: Any) -> str:
    """Normalize an LLM provider response to plain text.

    ``chat_completion_sync`` returns a plain ``str`` when no tools are
    requested, but may return a richer object (e.g.
    :class:`~doctoragent.model.provider.ChatCompletionResponse`) in other
    configurations. This helper hides that difference from callers.
    """
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    return getattr(response, "content", "") or ""


def _relation_id(source: str, target: str, relation_type: str) -> str:
    """Stable identifier for a (source, target, type) relation triple."""
    raw = f"{source}\x1f{target}\x1f{relation_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Entity:
    """A node in the knowledge graph.

    ``source_doc_ids`` records which chunks/documents mentioned this entity,
    enabling graph-based retrieval to map an entity back to source passages.
    """

    name: str
    entity_type: str = "concept"
    properties: dict[str, Any] = dc_field(default_factory=dict)
    source_doc_ids: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize this entity to a plain dict (JSON-safe)."""
        return {
            "name": self.name,
            "entity_type": self.entity_type,
            "properties": dict(self.properties),
            "source_doc_ids": list(self.source_doc_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entity:
        """Reconstruct an :class:`Entity` from a serialized dict."""
        return cls(
            name=data.get("name", ""),
            entity_type=data.get("entity_type", "concept"),
            properties=dict(data.get("properties") or {}),
            source_doc_ids=list(data.get("source_doc_ids") or []),
        )


@dataclass
class Relation:
    """A directed edge between two entities."""

    source: str
    target: str
    relation_type: str
    properties: dict[str, Any] = dc_field(default_factory=dict)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize this relation to a plain dict (JSON-safe)."""
        return {
            "source": self.source,
            "target": self.target,
            "relation_type": self.relation_type,
            "properties": dict(self.properties),
            "confidence": float(self.confidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Relation:
        """Reconstruct a :class:`Relation` from a serialized dict."""
        return cls(
            source=data.get("source", ""),
            target=data.get("target", ""),
            relation_type=data.get("relation_type", "related_to"),
            properties=dict(data.get("properties") or {}),
            confidence=float(data.get("confidence", 1.0)),
        )


# ---------------------------------------------------------------------------
# Knowledge Graph
# ---------------------------------------------------------------------------


class KnowledgeGraph:
    """Persisted knowledge graph over the vault corpus.

    The graph is backed by SQLite (entities + relations tables) and mirrors
    its contents into memory for fast traversal. Writes are serialised by a
    lock, matching the :class:`TaskStore` pattern.
    """

    def __init__(
        self,
        db_path: Path,
        tenant_id: str = "default",
    ) -> None:
        """Initialize the graph store.

        Args:
            db_path: Path to the SQLite database file (created if missing).
            tenant_id: Multi-tenant isolation key. All writes are tagged
                with it and all reads are filtered by it.
        """
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.tenant_id = tenant_id
        self._write_lock = threading.Lock()
        # In-memory mirror for cheap traversal.
        self._entities: dict[str, Entity] = {}
        self._relations: list[Relation] = []
        # Optional TaskStore reference used to hydrate chunk text during
        # retrieval. Attached via :meth:`attach_task_store`.
        self._task_store: Any | None = None
        self._init_db()
        self._load_into_memory()

    # ------------------------------------------------------------------
    # SQLite plumbing
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection configured for concurrent use."""
        return open_sqlite(self.db_path)

    def _init_db(self) -> None:
        """Create the entity / relation tables and indexes if missing."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_entities (
                    name TEXT PRIMARY KEY,
                    entity_type TEXT,
                    properties TEXT,
                    source_doc_ids TEXT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_relations (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    target TEXT,
                    relation_type TEXT,
                    properties TEXT,
                    confidence REAL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    created_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kg_ent_tenant ON kg_entities(tenant_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kg_rel_source ON kg_relations(source, tenant_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kg_rel_target ON kg_relations(target, tenant_id)"
            )
            conn.commit()

    def _load_into_memory(self) -> None:
        """Hydrate the in-memory mirror from SQLite (called on init)."""
        self._entities.clear()
        self._relations.clear()
        try:
            with self._connect() as conn:
                ent_rows = conn.execute(
                    "SELECT name, entity_type, properties, source_doc_ids "
                    "FROM kg_entities WHERE tenant_id = ?",
                    (self.tenant_id,),
                ).fetchall()
                for row in ent_rows:
                    self._entities[row[0]] = Entity(
                        name=row[0],
                        entity_type=row[1] or "concept",
                        properties=json.loads(row[2]) if row[2] else {},
                        source_doc_ids=json.loads(row[3]) if row[3] else [],
                    )
                rel_rows = conn.execute(
                    "SELECT source, target, relation_type, properties, confidence "
                    "FROM kg_relations WHERE tenant_id = ?",
                    (self.tenant_id,),
                ).fetchall()
                for row in rel_rows:
                    self._relations.append(
                        Relation(
                            source=row[0],
                            target=row[1],
                            relation_type=row[2] or "related_to",
                            properties=json.loads(row[3]) if row[3] else {},
                            confidence=float(row[4]) if row[4] is not None else 1.0,
                        )
                    )
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("Failed to load knowledge graph into memory: %s", exc)

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------

    def add_entity(self, entity: Entity) -> None:
        """Add or merge an entity into the graph.

        If an entity with the same name already exists, its
        ``source_doc_ids`` are unioned and ``properties`` merged (existing
        keys take precedence to avoid clobbering curated metadata).
        """
        if not entity.name:
            return
        now = datetime.now(UTC).isoformat()
        with self._write_lock, self._connect() as conn:
            existing = self._entities.get(entity.name)
            if existing is not None:
                merged_docs = list(dict.fromkeys(existing.source_doc_ids + entity.source_doc_ids))
                merged_props = {**entity.properties, **existing.properties}
                merged = Entity(
                    name=existing.name,
                    entity_type=existing.entity_type,
                    properties=merged_props,
                    source_doc_ids=merged_docs,
                )
            else:
                merged = entity
            self._entities[entity.name] = merged
            conn.execute(
                """
                INSERT OR REPLACE INTO kg_entities
                    (name, entity_type, properties, source_doc_ids, tenant_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    merged.name,
                    merged.entity_type,
                    json.dumps(merged.properties, ensure_ascii=False),
                    json.dumps(merged.source_doc_ids, ensure_ascii=False),
                    self.tenant_id,
                    now,
                ),
            )
            conn.commit()

    def add_relation(self, relation: Relation) -> None:
        """Add a relation (directed edge) to the graph.

        Duplicate triples (same source/target/type) upsert: properties are
        merged and the confidence is set to the maximum of the old and new
        value (a second sighting should not weaken confidence).

        Both endpoints are ensured to exist as graph nodes: when a relation
        references an entity that has not been added explicitly, a minimal
        stub entity is created (``INSERT OR IGNORE`` so existing, richer
        entities are never clobbered). This keeps the graph traversable even
        when relations are added directly.
        """
        if not relation.source or not relation.target:
            return
        rel_id = _relation_id(relation.source, relation.target, relation.relation_type)
        now = datetime.now(UTC).isoformat()
        with self._write_lock, self._connect() as conn:
            # Merge with an existing identical triple if present.
            existing = next(
                (
                    r
                    for r in self._relations
                    if r.source == relation.source
                    and r.target == relation.target
                    and r.relation_type == relation.relation_type
                ),
                None,
            )
            if existing is not None:
                merged_props = {**existing.properties, **relation.properties}
                merged_conf = max(existing.confidence, relation.confidence)
                merged = Relation(
                    source=existing.source,
                    target=existing.target,
                    relation_type=existing.relation_type,
                    properties=merged_props,
                    confidence=merged_conf,
                )
                self._relations.remove(existing)
            else:
                merged = relation
            self._relations.append(merged)
            # Ensure both endpoints exist as graph nodes (minimal stubs).
            for name in (merged.source, merged.target):
                if name and name not in self._entities:
                    stub = Entity(name=name, entity_type="concept")
                    self._entities[name] = stub
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO kg_entities
                            (name, entity_type, properties, source_doc_ids,
                             tenant_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            stub.name,
                            stub.entity_type,
                            json.dumps(stub.properties, ensure_ascii=False),
                            json.dumps(stub.source_doc_ids, ensure_ascii=False),
                            self.tenant_id,
                            now,
                        ),
                    )
            conn.execute(
                """
                INSERT OR REPLACE INTO kg_relations
                    (id, source, target, relation_type, properties, confidence,
                     tenant_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rel_id,
                    merged.source,
                    merged.target,
                    merged.relation_type,
                    json.dumps(merged.properties, ensure_ascii=False),
                    float(merged.confidence),
                    self.tenant_id,
                    now,
                ),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get_entity(self, name: str) -> Entity | None:
        """Return the entity named *name*, or ``None`` if absent."""
        return self._entities.get(name)

    def get_relations(self, entity_name: str | None = None) -> list[Relation]:
        """Return relations.

        When *entity_name* is given, only relations where the entity is the
        ``source`` or ``target`` are returned. Otherwise every relation is
        returned.
        """
        if entity_name is None:
            return list(self._relations)
        return [r for r in self._relations if r.source == entity_name or r.target == entity_name]

    def get_subgraph(
        self,
        entity_name: str,
        depth: int = 2,
    ) -> dict[str, Any]:
        """Return the subgraph within *depth* hops of *entity_name*.

        Performs a breadth-first traversal from the seed entity and collects
        all reachable entities and the relations traversed.

        Returns a dict::

            {"seed": entity_name, "entities": [Entity, ...],
             "relations": [Relation, ...]}
        """
        if depth < 0:
            depth = 0
        visited: set[str] = set()
        seen_rel_ids: set[str] = set()
        out_entities: list[Entity] = []
        out_relations: list[Relation] = []

        if entity_name not in self._entities:
            return {"seed": entity_name, "entities": [], "relations": []}

        queue: deque[tuple[str, int]] = deque([(entity_name, 0)])
        visited.add(entity_name)
        out_entities.append(self._entities[entity_name])

        while queue:
            current, d = queue.popleft()
            if d >= depth:
                continue
            for rel in self.get_relations(current):
                rid = _relation_id(rel.source, rel.target, rel.relation_type)
                if rid not in seen_rel_ids:
                    seen_rel_ids.add(rid)
                    out_relations.append(rel)
                neighbor = rel.target if rel.source == current else rel.source
                if neighbor not in visited and neighbor in self._entities:
                    visited.add(neighbor)
                    out_entities.append(self._entities[neighbor])
                    queue.append((neighbor, d + 1))

        return {
            "seed": entity_name,
            "entities": out_entities,
            "relations": out_relations,
        }

    # ------------------------------------------------------------------
    # LLM-driven extraction
    # ------------------------------------------------------------------

    def extract_entities_and_relations(
        self,
        text: str,
        llm_provider: Any | None,
        doc_id: str | None = None,
    ) -> tuple[list[Entity], list[Relation]]:
        """Extract entities and relations from *text* using an LLM.

        The LLM is asked to return a JSON object of the shape::

            {"entities": [{"name", "type", "properties"}],
             "relations": [{"source", "target", "type", "confidence"}]}

        Returns two empty lists when no LLM is available or parsing fails.
        ``doc_id`` is stamped onto every extracted entity's
        ``source_doc_ids`` so retrieval can map entities back to passages.
        """
        if llm_provider is None or not text or not text.strip():
            return [], []

        prompt = self._extraction_prompt(text)
        try:
            messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            response = llm_provider.chat_completion_sync(messages)
            data = _extract_json(_llm_response_text(response))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Knowledge graph extraction LLM call failed: %s", exc)
            return [], []

        if not isinstance(data, dict):
            return [], []

        entities: list[Entity] = []
        for raw in data.get("entities", []) or []:
            if not isinstance(raw, dict):
                continue
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            entities.append(
                Entity(
                    name=name,
                    entity_type=(raw.get("type") or raw.get("entity_type") or "concept").strip()
                    if isinstance(raw.get("type") or raw.get("entity_type"), str)
                    else "concept",
                    properties=dict(raw.get("properties") or {}),
                    source_doc_ids=[doc_id] if doc_id else [],
                )
            )

        # Build a set of known entity names for relation filtering.
        known = {e.name for e in entities}

        relations: list[Relation] = []
        for raw in data.get("relations", []) or []:
            if not isinstance(raw, dict):
                continue
            source = (raw.get("source") or "").strip()
            target = (raw.get("target") or "").strip()
            rtype = (raw.get("type") or raw.get("relation_type") or "related_to").strip()
            if not source or not target:
                continue
            try:
                confidence = float(raw.get("confidence", 1.0))
            except (TypeError, ValueError):
                confidence = 1.0
            confidence = max(0.0, min(1.0, confidence))
            relations.append(
                Relation(
                    source=source,
                    target=target,
                    relation_type=rtype if isinstance(rtype, str) else "related_to",
                    properties=dict(raw.get("properties") or {}),
                    confidence=confidence,
                )
            )
            # Ensure both endpoints exist as entities (self-loops included).
            known.add(source)
            known.add(target)

        # Add implicit entities discovered only via relations so the graph
        # stays connected even if the LLM omitted them from the entity list.
        for rel in relations:
            for name in (rel.source, rel.target):
                if name and not any(e.name == name for e in entities):
                    entities.append(
                        Entity(
                            name=name,
                            entity_type="concept",
                            source_doc_ids=[doc_id] if doc_id else [],
                        )
                    )

        return entities, relations

    def _extraction_prompt(self, text: str) -> str:
        """Build the LLM prompt for entity/relation extraction."""
        return (
            "You are a knowledge-graph construction engine. Extract the named "
            "entities and the relations between them from the text below.\n\n"
            "Return ONLY a JSON object with this exact schema (no prose):\n"
            "{\n"
            '  "entities": [\n'
            '    {"name": "<entity name>", "type": "<person|org|concept|'
            'location|event|document|other>", "properties": {"...": "..."}}\n'
            "  ],\n"
            '  "relations": [\n'
            '    {"source": "<entity name>", "target": "<entity name>", '
            '"type": "<relation type>", "confidence": 0.0-1.0, '
            '"properties": {"...": "..."}}\n'
            "  ]\n"
            "}\n\n"
            "Rules:\n"
            "- Entity names must be canonical and consistent across relations.\n"
            "- Relation type should be a short verb phrase (e.g. "
            '"works_for", "located_in", "depends_on", "part_of").\n'
            "- confidence is a float between 0 and 1.\n\n"
            f"Text:\n{text}\n"
        )

    # ------------------------------------------------------------------
    # Bulk construction
    # ------------------------------------------------------------------

    def attach_task_store(self, task_store: Any) -> None:
        """Attach a TaskStore used to hydrate chunk text during retrieval."""
        self._task_store = task_store

    def build_from_documents(
        self,
        task_store: Any,
        llm_provider: Any | None,
    ) -> dict[str, int]:
        """Build the graph by extracting entities/relations from every chunk.

        Reads all chunks from *task_store*'s ``vault_chunks`` table, runs
        extraction on each chunk's text, and ingests the results. Already-
        known entities/relations are merged rather than duplicated.

        Returns a small stats dict with counts.
        """
        chunks = self._load_chunks(task_store)
        logger.info(
            "Building knowledge graph from %d chunk(s) (tenant=%s)",
            len(chunks),
            self.tenant_id,
        )
        entity_count = 0
        relation_count = 0
        for chunk in chunks:
            text = chunk.get("text") or ""
            doc_id = chunk.get("chunk_id") or chunk.get("task_id") or ""
            if not text.strip():
                continue
            try:
                entities, relations = self.extract_entities_and_relations(
                    text,
                    llm_provider,
                    doc_id=doc_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("KG extraction failed for chunk %s: %s", doc_id, exc)
                continue
            for entity in entities:
                self.add_entity(entity)
            for relation in relations:
                self.add_relation(relation)
            entity_count += len(entities)
            relation_count += len(relations)

        stats = {
            "chunks_processed": len(chunks),
            "entities_extracted": entity_count,
            "relations_extracted": relation_count,
            "total_entities": len(self._entities),
            "total_relations": len(self._relations),
        }
        logger.info("Knowledge graph build complete: %s", stats)
        return stats

    def _load_chunks(self, task_store: Any) -> list[dict[str, Any]]:
        """Load all chunk rows from *task_store* (best-effort, resilient)."""
        tenant_id = (
            getattr(task_store, "_tenant_id", None)
            or getattr(task_store, "tenant_id", None)
            or self.tenant_id
        )
        db_path = getattr(task_store, "db_path", None)
        if db_path is None:
            # Fall back to the task_store's own connection helper if present.
            connect = getattr(task_store, "_connect", None)
            if connect is None:
                return []
            try:
                with connect() as conn:
                    rows = conn.execute(
                        "SELECT chunk_id, task_id, text FROM vault_chunks "
                        "WHERE tenant_id = ? AND text IS NOT NULL AND text != ''",
                        (tenant_id,),
                    ).fetchall()
            except (sqlite3.Error, AttributeError):
                return []
            return [{"chunk_id": r[0], "task_id": r[1], "text": r[2]} for r in rows]

        try:
            with open_sqlite(db_path) as conn:
                rows = conn.execute(
                    "SELECT chunk_id, task_id, text FROM vault_chunks "
                    "WHERE tenant_id = ? AND text IS NOT NULL AND text != ''",
                    (tenant_id,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("Failed to load chunks for KG build: %s", exc)
            return []
        return [{"chunk_id": r[0], "task_id": r[1], "text": r[2]} for r in rows]

    # ------------------------------------------------------------------
    # Graph-based retrieval
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        llm_provider: Any | None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Graph-based retrieval.

        Extracts the entities mentioned in *query*, traverses the graph to
        gather related entities (one hop by default), and returns the source
        chunks (``source_doc_ids``) associated with those entities.

        Each result dict carries ``chunk_id``, ``score`` (proximity-weighted),
        ``matched_entities`` (the entities that surfaced it) and, when a
        task store is attached, the hydrated ``text``.
        """
        if not query or not query.strip():
            return []

        query_entities = self._extract_query_entities(query, llm_provider)
        if not query_entities:
            return []

        # chunk_id -> (best_score, set(matched entities))
        chunk_scores: dict[str, tuple[float, set[str]]] = defaultdict(lambda: (0.0, set()))

        for ent_name in query_entities:
            # Direct hit: entity mentioned in the query.
            entity = self._entities.get(ent_name)
            if entity is not None:
                self._accumulate(chunk_scores, entity.source_doc_ids, 1.0, ent_name)
                # One-hop neighbors contribute with decayed weight.
                for rel in self.get_relations(ent_name):
                    neighbor = rel.target if rel.source == ent_name else rel.source
                    nb = self._entities.get(neighbor)
                    if nb is not None:
                        self._accumulate(
                            chunk_scores,
                            nb.source_doc_ids,
                            0.5 * rel.confidence,
                            neighbor,
                        )

        ranked = sorted(
            chunk_scores.items(),
            key=lambda kv: kv[1][0],
            reverse=True,
        )[:top_k]

        results: list[dict[str, Any]] = []
        for chunk_id, (score, matched) in ranked:
            entry: dict[str, Any] = {
                "chunk_id": chunk_id,
                "score": round(score, 6),
                "matched_entities": sorted(matched),
                "source": "graph",
            }
            hydrated = self._hydrate_chunk(chunk_id)
            if hydrated is not None:
                entry.update(hydrated)
            results.append(entry)
        return results

    def _accumulate(
        self,
        chunk_scores: dict[str, tuple[float, set[str]]],
        doc_ids: list[str],
        weight: float,
        entity_name: str,
    ) -> None:
        """Merge a weighted contribution into the chunk score map."""
        for cid in doc_ids:
            if not cid:
                continue
            prev_score, prev_matched = chunk_scores[cid]
            chunk_scores[cid] = (
                max(prev_score, weight),
                prev_matched | {entity_name},
            )

    def _extract_query_entities(
        self,
        query: str,
        llm_provider: Any | None,
    ) -> list[str]:
        """Return entity names from *query* known to the graph.

        Uses LLM extraction first; falls back to substring matching of known
        entity names when no LLM is available or extraction yields nothing.
        """
        llm_entities: list[str] = []
        if llm_provider is not None:
            try:
                entities, _ = self.extract_entities_and_relations(query, llm_provider)
                llm_entities = [e.name for e in entities if e.name in self._entities]
            except Exception as exc:  # noqa: BLE001
                logger.debug("Query entity extraction failed: %s", exc)

        if llm_entities:
            return llm_entities

        # Heuristic fallback: match known entity names appearing in the query.
        qlower = query.lower()
        matched = [name for name in self._entities if name and name.lower() in qlower]
        return matched

    def _hydrate_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        """Load chunk text/metadata from the attached task store if any."""
        if self._task_store is None:
            return None
        db_path = getattr(self._task_store, "db_path", None)
        tenant_id = (
            getattr(self._task_store, "_tenant_id", None)
            or getattr(self._task_store, "tenant_id", None)
            or self.tenant_id
        )
        if db_path is None:
            return None
        try:
            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT task_id, vault_path, text FROM vault_chunks "
                    "WHERE chunk_id = ? AND tenant_id = ?",
                    (chunk_id, tenant_id),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        return {"task_id": row[0], "vault_path": row[1], "text": row[2]}

    # ------------------------------------------------------------------
    # Merge / serialization
    # ------------------------------------------------------------------

    def merge_graph(self, other: KnowledgeGraph) -> None:
        """Merge *other* into this graph.

        Entities are unioned (source_doc_ids merged) and relations are
        de-duplicated by (source, target, type).
        """
        for entity in other._entities.values():
            self.add_entity(entity)
        for relation in other._relations:
            self.add_relation(relation)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the whole graph to a JSON-safe dict."""
        return {
            "tenant_id": self.tenant_id,
            "entities": [e.to_dict() for e in self._entities.values()],
            "relations": [r.to_dict() for r in self._relations],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        db_path: Path,
        tenant_id: str | None = None,
    ) -> KnowledgeGraph:
        """Reconstruct a :class:`KnowledgeGraph` from a serialized dict.

        A new SQLite store is created at *db_path* and the entities/relations
        from *data* are written into it.
        """
        tid = tenant_id or data.get("tenant_id") or "default"
        graph = cls(db_path, tenant_id=tid)
        for raw in data.get("entities", []) or []:
            graph.add_entity(Entity.from_dict(raw))
        for raw in data.get("relations", []) or []:
            graph.add_relation(Relation.from_dict(raw))
        return graph
