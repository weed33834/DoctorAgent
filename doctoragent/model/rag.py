"""Cutting-edge RAG (Retrieval-Augmented Generation) module for DoctorAgent.

This module implements the most advanced RAG and context engineering techniques:

1. **Context Engineering**: Multi-layered context assembly
   - System instructions
   - Memory context (short-term, long-term, episodic)
   - Retrieved documents with citations
   - Conversation history
   - User query

2. **Memory System**: Four-layer memory architecture
   - Short-term: Conversation buffer (sliding window)
   - Working: Current task context
   - Episodic: Past interactions (vector search)
   - Long-term: Persistent facts and preferences

3. **Advanced RAG Techniques**:
   - Query transformation (rewriting, expansion, decomposition)
   - Hybrid retrieval (BM25 + Dense + RRF)
   - Cross-encoder re-ranking
   - Context compression and filtering
   - Multi-hop reasoning

4. **Conversation Management**:
   - Sliding window truncation
   - Summarization pipeline
   - Token budget management
   - Context ordering

5. **Prompt Engineering**:
   - System prompts with role definition
   - Few-shot examples
   - Chain-of-thought reasoning
   - Structured output formatting

Architecture:
    Query → Query Transformation → Hybrid Retrieval → Re-ranking →
    Context Assembly (Memory + RAG + History) → LLM Generation → Answer

Key innovations:
- Four-layer memory system for coherent conversations
- Context engineering with token budget management
- Multi-hop reasoning for complex queries
- Self-reflection and answer verification
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import struct
import threading
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from doctoragent._utils import (
    cosine_similarity as _cosine_similarity,
)
from doctoragent._utils import (
    cosine_similarity_matrix as _cosine_similarity_matrix,
)
from doctoragent._utils import (
    open_sqlite,
)
from doctoragent.compat import UTC

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# tiktoken-based token counting (with fallback)
# ---------------------------------------------------------------------------

# Cached tiktoken encoding — loaded lazily on first use.  When tiktoken is
# unavailable (or the encoding download fails) we fall back to the original
# ``len(text) // 4`` heuristic.
_tiktoken_encoding: Any | None = None
_tiktoken_loaded = False


def _count_tokens(text: str) -> int:
    """Count tokens in *text* using tiktoken, falling back to ``len // 4``.

    Uses the ``cl100k_base`` encoding (GPT-4 / ChatGPT default).  The
    encoding object is cached after the first successful load.  If tiktoken
    is not installed or the encoding cannot be loaded, the function
    transparently falls back to the original character-based heuristic.
    """
    global _tiktoken_encoding, _tiktoken_loaded
    if not _tiktoken_loaded:
        _tiktoken_loaded = True
        try:
            import tiktoken

            _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001 — ImportError or download failure
            _tiktoken_encoding = None
    if _tiktoken_encoding is not None:
        try:
            return len(_tiktoken_encoding.encode(text))
        except Exception:  # noqa: BLE001
            pass
    return max(1, len(text) // 4)


def _split_fact_candidates(text: str) -> list[str]:
    """Split a text into sentence-length fact candidates.

    Used by memory consolidation as a heuristic fallback when no extractor is
    supplied. Splits on sentence boundaries and drops very short fragments.
    """
    if not text:
        return []
    import re

    candidates = re.split(r"(?<=[。！？.!?；;])\s*", text)
    return [c.strip() for c in candidates if c.strip()]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Context engineering constants
MAX_CONTEXT_TOKENS = 4000  # Conservative estimate for context window
TOKENS_PER_CHAR = 0.25  # Approximate tokens per character (English)
MAX_HISTORY_TURNS = 10  # Maximum conversation turns to keep
SUMMARY_THRESHOLD = 5  # Summarize after this many turns
COMPRESSION_RATIO = 0.5  # Compress context to 50% when over budget

# Chunk sizes
DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 64

# Retrieval
INITIAL_RETRIEVAL_K = 20
FINAL_K = 5

# Cross-encoder model
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# ---------------------------------------------------------------------------
# RAG configuration switches & tuning constants
# ---------------------------------------------------------------------------
# All new capabilities are opt-in so that the default pipeline behaviour is
# byte-for-byte identical to the previous "basic" implementation. Operators
# enable them through :class:`RagConfig` (or the matching ``ask()`` flags).

# ANN index: brute-force (vectorised numpy) is preferred below this many
# vectors; above it an approximate index is built to keep latency sub-linear.
ANN_VECTOR_THRESHOLD = 1000

# Semantic de-duplication: chunks whose cosine similarity to an already-kept
# chunk exceeds this threshold are treated as near-duplicates and dropped.
SEMANTIC_DEDUP_THRESHOLD = 0.92

# HyDE / multi-query: how many hypothetical / rewritten queries to generate.
HYDE_DOC_COUNT = 1
MULTI_QUERY_COUNT = 3

# Recursive retrieval: how many top documents to drill into for chunks.
RECURSIVE_DOC_TOP_K = 5

# Sub-question engine: trigger decomposition when the LLM judges the question
# complex. These bound the number of sub-questions answered in parallel.
SUBQUESTION_MIN = 2
SUBQUESTION_MAX = 4

# Response synthesis auto-selection thresholds (chunk counts).
SYNTHESIS_COMPACT_MAX = 3  # <= 3 chunks -> single Compact pass
SYNTHESIS_REFINE_MAX = 8  # 4..8 chunks -> Refine
# > 8 chunks  -> Tree Summarize

# Context compression: only invoke the LLM compressor when the assembled
# retrieved context exceeds this many tokens.
COMPRESSION_TRIGGER_TOKENS = 2500


@dataclass
class RagConfig:
    """Feature flags and tuning knobs for the RAG pipeline.

    Every flag defaults to ``False``/``"compact"`` so that constructing a
    :class:`RagPipeline` with no config reproduces the legacy behaviour
    exactly. New capabilities are turned on explicitly by the operator.
    """

    # Query transformation
    enable_hyde: bool = False
    enable_step_back: bool = False
    enable_multi_query: bool = False
    enable_subquestion: bool = False

    # Retrieval
    enable_ann_index: bool = True
    ann_threshold: int = ANN_VECTOR_THRESHOLD
    ann_mode: str = "auto"  # "auto" | "numpy" | "lsh" | "brute"
    ann_persist: bool = True
    enable_recursive_retrieval: bool = False
    enable_parent_child: bool = False

    # Vector store backend (P2 scalability hook).
    # The default ``"sqlite"`` keeps the legacy in-process behaviour
    # (numpy cosine over ``vault_vectors``). Setting this to ``"chroma"``
    # (or any future backend name) selects a horizontally-scalable backend
    # via :mod:`doctoragent.model.vectorstore`. Retrieval-core integration is
    # intentionally NOT forced here: when a non-default backend is selected
    # the retriever logs a warning and continues to use the SQLite path
    # until a follow-up wires the new backend into the dense retriever.
    vector_backend: str = "sqlite"
    # Persistence path for the alternative backend (e.g. Chroma's directory).
    # ``None`` means "use the default location derived from db_path".
    vector_backend_path: str | None = None

    # Context engineering
    enable_semantic_dedup: bool = False
    semantic_dedup_threshold: float = SEMANTIC_DEDUP_THRESHOLD
    enable_context_compression: bool = False
    enable_importance_weighting: bool = False

    # Response synthesis
    synthesis_strategy: str = "compact"  # "compact" | "refine" | "tree" | "auto"

    # Evaluation
    enable_rag_evaluation: bool = False

    # Streaming
    enable_streaming: bool = True


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


class MemoryEntry(BaseModel):
    """A single memory entry."""

    memory_id: str = ""
    content: str = ""
    memory_type: str = "episodic"  # episodic, semantic, procedural
    importance: float = 0.5  # 0.0 to 1.0
    created_at: str = ""
    last_accessed: str = ""
    access_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationTurn(BaseModel):
    """A single conversation turn."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: str = ""
    tokens: int = 0


class ConversationMemory(BaseModel):
    """Conversation history with management."""

    turns: list[ConversationTurn] = Field(default_factory=list)
    summary: str = ""
    key_facts: list[str] = Field(default_factory=list)
    total_tokens: int = 0


class ContextWindow(BaseModel):
    """Assembled context window for LLM."""

    system_prompt: str = ""
    memory_context: str = ""
    retrieved_context: str = ""
    conversation_history: str = ""
    user_query: str = ""
    total_tokens: int = 0
    sources: list[dict[str, Any]] = Field(default_factory=list)
    memory_used: bool = False
    conversation_turns: int = 0


class RagResult(BaseModel):
    """A single RAG retrieval result."""

    chunk: dict[str, Any] = Field(default_factory=dict)
    score: float = 0.0
    rank: int = 0
    source_label: str = ""


class RagResponse(BaseModel):
    """Full RAG response with answer and citations."""

    answer: str
    question: str
    sources: list[RagResult] = Field(default_factory=list)
    model_used: str = ""
    retrieval_method: str = "hybrid"
    total_chunks_searched: int = 0
    context_tokens_used: int = 0
    memory_used: bool = False
    conversation_turns: int = 0
    session_id: str | None = None
    # Retrieval/generation quality metrics populated when
    # ``RagConfig.enable_rag_evaluation`` is on. Keys are metric names
    # (context_precision, faithfulness, ...) mapped to their scores, plus a
    # composite ``rag_score``. Empty by default for full backward compat.
    evaluation_metrics: dict[str, Any] = Field(default_factory=dict)
    # Which response-synthesis strategy produced this answer.
    synthesis_strategy: str = "compact"


# ---------------------------------------------------------------------------
# Memory System
# ---------------------------------------------------------------------------


class MemorySystem:
    """Four-layer memory system for coherent conversations.

    Memory layers:
    1. Short-term: Current conversation buffer (sliding window)
    2. Working: Current task context
    3. Episodic: Past interactions (vector search)
    4. Long-term: Persistent facts and preferences
    """

    def __init__(
        self,
        db_path: Path,
        tenant_id: str = "default",
        embedding_provider: Any | None = None,
    ) -> None:
        self.db_path = db_path
        self.tenant_id = tenant_id
        # Optional embedding provider upgrades episodic recall from keyword
        # matching to vector similarity search. When ``None`` (the default)
        # recall falls back to the legacy keyword-overlap heuristic, keeping
        # the previous behaviour for callers that pass no provider.
        self.embedding_provider = embedding_provider
        # 记忆清理配置
        self._max_facts = 5000  # 长期事实最大数量
        self._max_episodes = 2000  # 情节记忆最大数量
        self._fact_ttl_days = 90  # 事实TTL（天），超过后自动衰减
        self._episode_ttl_days = 180  # 情节记忆TTL（天）
        self._low_importance_threshold = 0.2  # 重要性低于此值且过期时优先清理
        self._cleanup_interval = 50  # 每50次写入自动清理一次
        self._write_count = 0  # 写入计数器
        # Long-horizon memory consolidation: every N episodes the semantic
        # compaction pass runs, distilling episodic memory into long-term facts
        # (episodic → semantic, M5.11) so durable knowledge survives the
        # episode-level TTL / forgetting (M5.12).
        self._consolidation_interval = 20
        self._episodes_since_consolidation = 0
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection."""
        return open_sqlite(self.db_path)

    def _init_db(self) -> None:
        """Create memory tables if not exists."""
        with self._connect() as conn:
            # Long-term memory (facts, preferences)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_long_term (
                    memory_id TEXT PRIMARY KEY,
                    content TEXT,
                    memory_type TEXT,
                    importance REAL,
                    created_at TEXT,
                    last_accessed TEXT,
                    access_count INTEGER DEFAULT 0,
                    metadata TEXT,
                    tenant_id TEXT NOT NULL DEFAULT 'default'
                )
                """
            )

            # Episodic memory (past interactions)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_episodic (
                    memory_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    user_message TEXT,
                    assistant_response TEXT,
                    context_summary TEXT,
                    key_facts TEXT,
                    timestamp TEXT,
                    embedding BLOB,
                    tenant_id TEXT NOT NULL DEFAULT 'default'
                )
                """
            )

            # Conversation sessions
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    session_id TEXT PRIMARY KEY,
                    started_at TEXT,
                    last_active TEXT,
                    turn_count INTEGER DEFAULT 0,
                    summary TEXT,
                    tenant_id TEXT NOT NULL DEFAULT 'default'
                )
                """
            )

            # Conversation turns
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    timestamp TEXT,
                    tokens INTEGER DEFAULT 0,
                    tenant_id TEXT NOT NULL DEFAULT 'default'
                )
                """
            )

            conn.commit()

        self._ensure_episode_consolidated_column()

    def _ensure_episode_consolidated_column(self) -> None:
        """Add the ``consolidated`` marker column to memory_episodic if missing.

        Existing deployments created the table without this column; adding it
        lazily lets consolidation track which episodes have already been
        compacted into long-term semantic memory without a migration step.
        """
        try:
            with self._connect() as conn:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_episodic)")}
                if "consolidated" not in cols:
                    conn.execute(
                        "ALTER TABLE memory_episodic ADD COLUMN consolidated INTEGER DEFAULT 0"
                    )
                    conn.commit()
        except Exception:  # noqa: BLE001 — consolidation is best-effort
            pass

    # --- Long-term Memory ---

    def store_fact(
        self,
        content: str,
        memory_type: str = "semantic",
        importance: float = 0.5,
        metadata: dict | None = None,
    ) -> str:
        """Store a fact in long-term memory."""
        memory_id = hashlib.sha256(content.encode()).hexdigest()[:16]
        now = datetime.now(UTC).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_long_term
                    (memory_id, content, memory_type, importance, created_at,
                     last_accessed, access_count, metadata, tenant_id)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    memory_id,
                    content,
                    memory_type,
                    importance,
                    now,
                    now,
                    json.dumps(metadata or {}),
                    self.tenant_id,
                ),
            )
            conn.commit()

        # 每 N 次写入自动清理
        self._write_count += 1
        if self._write_count >= self._cleanup_interval:
            self._decay_importance()
            self.prune_memories()
            self._write_count = 0
        return memory_id

    def recall_facts(self, query: str, limit: int = 5) -> list[MemoryEntry]:
        """Recall relevant facts from long-term memory.

        Uses a single batch ``UPDATE ... WHERE memory_id IN (...)`` to bump
        access counts for all recalled facts, avoiding the N+1 query pattern
        that opened a separate connection per row.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT memory_id, content, memory_type, importance, created_at,
                       last_accessed, access_count, metadata
                FROM memory_long_term
                WHERE tenant_id = ?
                ORDER BY importance DESC, last_accessed DESC
                LIMIT ?
                """,
                (self.tenant_id, limit),
            ).fetchall()

            if rows:
                # Batch update: increment access_count and update last_accessed
                # for all recalled facts in a single query (fixes N+1).
                # 召回时同时提升重要性（最多到 1.0），被成功召回的事实更不易被清理。
                now = datetime.now(UTC).isoformat()
                memory_ids = [row[0] for row in rows]
                placeholders = ",".join("?" * len(memory_ids))
                conn.execute(
                    f"UPDATE memory_long_term "  # nosec B608
                    f"SET access_count = access_count + 1, "
                    f"    importance = MIN(1.0, importance + 0.05), "
                    f"    last_accessed = ? "
                    f"WHERE memory_id IN ({placeholders})",
                    (now, *memory_ids),
                )
                conn.commit()

        entries = []
        for row in rows:
            entries.append(
                MemoryEntry(
                    memory_id=row[0],
                    content=row[1],
                    memory_type=row[2],
                    importance=row[3],
                    created_at=row[4],
                    last_accessed=row[5],
                    access_count=row[6],
                    metadata=json.loads(row[7]) if row[7] else {},
                )
            )
        return entries

    # --- Episodic Memory ---

    def store_episode(
        self,
        session_id: str,
        user_message: str,
        assistant_response: str,
        context_summary: str = "",
        key_facts: list[str] | None = None,
    ) -> str:
        """Store a conversation episode.

        When an embedding provider is configured, a dense vector is derived
        from the user message + response and persisted alongside the episode
        so that :meth:`recall_episodes` can use vector similarity instead of
        keyword overlap. Without a provider the embedding column stays NULL
        and recall degrades gracefully to the legacy keyword path.
        """
        memory_id = hashlib.sha256(f"{session_id}:{user_message[:100]}".encode()).hexdigest()[:16]
        now = datetime.now(UTC).isoformat()

        embedding_blob: bytes | None = None
        if self.embedding_provider is not None:
            try:
                text_for_embed = f"{user_message}\n{assistant_response}"
                vec = self.embedding_provider.embed([text_for_embed])[0]
                embedding_blob = struct.pack(f"{len(vec)}d", *vec)
            except Exception:  # noqa: BLE001 — embedding must never break storage
                embedding_blob = None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_episodic
                    (memory_id, session_id, user_message, assistant_response,
                     context_summary, key_facts, timestamp, embedding, tenant_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    session_id,
                    user_message,
                    assistant_response,
                    context_summary,
                    json.dumps(key_facts or []),
                    now,
                    embedding_blob,
                    self.tenant_id,
                ),
            )
            conn.commit()

        # Long-horizon consolidation: every N stored episodes run the episodic →
        # semantic compaction pass so durable knowledge survives episode TTL.
        self._episodes_since_consolidation += 1
        if self._episodes_since_consolidation >= self._consolidation_interval:
            self._episodes_since_consolidation = 0
            try:
                self.consolidate_memories()
            except Exception:  # noqa: BLE001 — consolidation must never break storage
                pass
        return memory_id

    def recall_episodes(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        """Recall relevant past episodes.

        When an embedding provider is configured, episodes are ranked by cosine
        similarity between the query embedding and the stored episode embedding
        (true semantic episodic-memory retrieval). Episodes lacking a stored
        embedding fall back to the keyword-overlap heuristic so historical data
        remains recallable after the upgrade. Without a provider the entire
        recall path is the original keyword-matching behaviour.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT memory_id, session_id, user_message, assistant_response,
                       context_summary, key_facts, timestamp, embedding
                FROM memory_episodic
                WHERE tenant_id = ?
                ORDER BY timestamp DESC
                """,
                (self.tenant_id,),
            ).fetchall()

        # Compute query embedding once (vector path).
        query_vector: list[float] | None = None
        if self.embedding_provider is not None and rows:
            try:
                query_vector = self.embedding_provider.embed([query])[0]
            except Exception:  # noqa: BLE001
                query_vector = None

        episodes: list[dict[str, Any]] = []
        query_lower = query.lower()
        for row in rows:
            relevance = 0.0
            if query_lower in (row[2] or "").lower():
                relevance += 0.5
            if query_lower in (row[3] or "").lower():
                relevance += 0.3
            if query_lower in (row[4] or "").lower():
                relevance += 0.2

            # Vector similarity path: override relevance with cosine score when
            # both query and episode embeddings are available.
            vector_score = 0.0
            if query_vector is not None and row[7] is not None:
                vec = self._read_episode_vector(row[7])
                if vec is not None:
                    vector_score = _cosine_similarity(query_vector, vec)
                    relevance = max(relevance, vector_score)

            if relevance > 0 or len(episodes) < limit:
                episodes.append(
                    {
                        "memory_id": row[0],
                        "session_id": row[1],
                        "user_message": row[2],
                        "assistant_response": row[3],
                        "context_summary": row[4],
                        "key_facts": json.loads(row[5]) if row[5] else [],
                        "timestamp": row[6],
                        "relevance": relevance,
                        "vector_score": vector_score,
                    }
                )

        # Sort by relevance (vector or keyword) then recency.
        episodes.sort(key=lambda x: (x["relevance"], x["timestamp"]), reverse=True)
        return episodes[:limit]

    @staticmethod
    def _read_episode_vector(blob: Any) -> list[float] | None:
        """Unpack a struct-packed episode embedding BLOB."""
        if blob is None:
            return None
        try:
            return list(struct.unpack(f"{len(blob) // 8}d", blob))
        except (struct.error, MemoryError):
            return None

    # --- Conversation Management ---

    def create_session(self) -> str:
        """Create a new conversation session."""
        session_id = hashlib.sha256(datetime.now(UTC).isoformat().encode()).hexdigest()[:16]
        now = datetime.now(UTC).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_sessions
                    (session_id, started_at, last_active, turn_count, tenant_id)
                VALUES (?, ?, ?, 0, ?)
                """,
                (session_id, now, now, self.tenant_id),
            )
            conn.commit()
        return session_id

    def add_turn(self, session_id: str, role: str, content: str) -> None:
        """Add a turn to the conversation."""
        now = datetime.now(UTC).isoformat()
        turn_id = hashlib.sha256(f"{session_id}:{role}:{content[:50]}:{now}".encode()).hexdigest()[
            :16
        ]
        tokens = _count_tokens(content)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_turns
                    (turn_id, session_id, role, content, timestamp, tokens, tenant_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (turn_id, session_id, role, content, now, tokens, self.tenant_id),
            )

            # Update session
            conn.execute(
                """
                UPDATE conversation_sessions
                SET last_active = ?, turn_count = turn_count + 1
                WHERE session_id = ? AND tenant_id = ?
                """,
                (now, session_id, self.tenant_id),
            )
            conn.commit()

    def get_conversation_history(
        self, session_id: str, max_turns: int = MAX_HISTORY_TURNS
    ) -> ConversationMemory:
        """Get conversation history with management."""
        with self._connect() as conn:
            # Get session info
            session = conn.execute(
                "SELECT summary FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

            # Get recent turns
            rows = conn.execute(
                """
                SELECT role, content, timestamp, tokens
                FROM conversation_turns
                WHERE session_id = ? AND tenant_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (session_id, self.tenant_id, max_turns),
            ).fetchall()

        turns = [
            ConversationTurn(role=row[0], content=row[1], timestamp=row[2], tokens=row[3])
            for row in reversed(rows)  # Reverse to get chronological order
        ]

        total_tokens = sum(t.tokens for t in turns)

        return ConversationMemory(
            turns=turns,
            summary=session[0] if session and session[0] else "",
            total_tokens=total_tokens,
        )

    def summarize_session(self, session_id: str, summary: str) -> None:
        """Store a summary for the session."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE conversation_sessions SET summary = ? WHERE session_id = ?",
                (summary, session_id),
            )
            conn.commit()

    # --- 记忆清理策略 ---

    def _decay_importance(self) -> None:
        """对所有长期事实进行重要性衰减。

        衰减规则:
        - 超过 TTL 的事实，重要性每天衰减 1%
        - 被成功召回的事实，重要性提升 0.05（最多到 1.0）
        - 重要性降到 0 以下且超过 TTL 的事实，标记为可清理
        """
        try:
            cutoff = datetime.now(UTC) - timedelta(days=self._fact_ttl_days)
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE memory_long_term
                    SET importance = MAX(0, importance - 0.01 * (
                        julianday('now') - julianday(created_at) - ?
                    ))
                    WHERE created_at < ?
                    """,
                    (self._fact_ttl_days, cutoff.isoformat()),
                )
                conn.commit()
        except Exception:
            pass

    def prune_memories(self, force: bool = False) -> dict:
        """清理过期/低重要性记忆。

        Returns:
            清理统计信息
        """
        stats = {"facts_removed": 0, "episodes_removed": 0}
        try:
            fact_cutoff = datetime.now(UTC) - timedelta(days=self._fact_ttl_days)
            episode_cutoff = datetime.now(UTC) - timedelta(days=self._episode_ttl_days)

            with self._connect() as conn:
                # 清理过期且重要性极低的事实
                stats["facts_removed"] = conn.execute(
                    """
                    DELETE FROM memory_long_term
                    WHERE created_at < ?
                      AND importance < ?
                    """,
                    (fact_cutoff.isoformat(), self._low_importance_threshold),
                ).rowcount

                # 清理过期的情节记忆（memory_episodic 使用 timestamp 字段）
                stats["episodes_removed"] = conn.execute(
                    """
                    DELETE FROM memory_episodic
                    WHERE timestamp < ?
                    """,
                    (episode_cutoff.isoformat(),),
                ).rowcount

                # 如果事实数量超过上限，删除重要性最低的
                count = conn.execute("SELECT COUNT(*) FROM memory_long_term").fetchone()[0]
                if count > self._max_facts:
                    excess = count - self._max_facts
                    stats["facts_removed"] += conn.execute(
                        """
                        DELETE FROM memory_long_term
                        WHERE memory_id IN (
                            SELECT memory_id FROM memory_long_term
                            ORDER BY importance ASC, last_accessed ASC
                            LIMIT ?
                        )
                        """,
                        (excess,),
                    ).rowcount

                # 如果情节记忆数量超过上限，删除最旧的（按 timestamp 排序）
                ep_count = conn.execute("SELECT COUNT(*) FROM memory_episodic").fetchone()[0]
                if ep_count > self._max_episodes:
                    excess = ep_count - self._max_episodes
                    stats["episodes_removed"] += conn.execute(
                        """
                        DELETE FROM memory_episodic
                        WHERE memory_id IN (
                            SELECT memory_id FROM memory_episodic
                            ORDER BY timestamp ASC
                            LIMIT ?
                        )
                        """,
                        (excess,),
                    ).rowcount

                conn.commit()
        except Exception:
            pass
        return stats

    # --- Long-horizon memory consolidation (episodic → semantic) ---

    def consolidate_memories(
        self,
        batch_size: int = 100,
        extractor: Any | None = None,
        prune_after: bool = True,
    ) -> dict[str, int]:
        """Compaction pass: distil un-consolidated episodes into semantic facts.

        Implements the **M5.11 / M5.12** long-horizon memory pipeline: episodic
        memories (past interactions) that have not yet been compacted are read
        oldest-first, reduced to durable *facts* (deduplicated against existing
        long-term memory), and stored as ``memory_type="semantic"`` facts so
        they survive the episode-level TTL / forgetting. Once consolidated, an
        episode is marked so the pass is idempotent.

        Args:
            batch_size:
                Maximum number of episodes to process per pass.
            extractor:
                Optional callable ``(user_message, assistant_response,
                context_summary) -> list[str]`` producing candidate facts. When
                ``None`` a heuristic is used that prefers the episode's stored
                ``key_facts`` and falls back to splitting the assistant response
                into sentences.
            prune_after:
                Whether to run :meth:`prune_memories` after consolidation so
                consolidated episodes age out and low-importance facts decay
                (enforces the forgetting half of M5.12).

        Returns:
            A stats dict: ``episodes_considered``, ``episodes_consolidated``,
            ``facts_added``, ``facts_skipped``.
        """
        stats = {
            "episodes_considered": 0,
            "episodes_consolidated": 0,
            "facts_added": 0,
            "facts_skipped": 0,
        }
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT memory_id, session_id, user_message, assistant_response,
                           context_summary, key_facts, timestamp
                    FROM memory_episodic
                    WHERE tenant_id = ? AND (consolidated IS NULL OR consolidated = 0)
                    ORDER BY timestamp ASC
                    LIMIT ?
                    """,
                    (self.tenant_id, batch_size),
                ).fetchall()
            if not rows:
                return stats
            stats["episodes_considered"] = len(rows)

            # Load existing fact texts once to dedup cheaply.
            with self._connect() as conn:
                existing = {
                    row[0]
                    for row in conn.execute(
                        "SELECT content FROM memory_long_term WHERE tenant_id = ?",
                        (self.tenant_id,),
                    ).fetchall()
                }

            consolidated_ids: list[str] = []
            for row in rows:
                memory_id, session_id, user_msg, assistant_resp, ctx_summary = row[:5]
                key_facts = json.loads(row[5]) if row[5] else []
                facts = self._extract_facts(
                    user_msg, assistant_resp, ctx_summary, key_facts, extractor
                )
                for fact in facts:
                    fact = (fact or "").strip()
                    if not fact:
                        continue
                    content_hash = hashlib.sha256(fact.encode()).hexdigest()[:16]
                    if content_hash in existing or fact in existing:
                        stats["facts_skipped"] += 1
                        continue
                    self.store_fact(
                        content=fact,
                        memory_type="semantic",
                        importance=0.6,  # consolidated knowledge is durable
                        metadata={"source": "consolidation", "session_id": session_id},
                    )
                    existing.add(fact)
                    existing.add(content_hash)
                    stats["facts_added"] += 1
                consolidated_ids.append(memory_id)

            if consolidated_ids:
                with self._connect() as conn:
                    conn.execute(
                        f"UPDATE memory_episodic SET consolidated = 1 "
                        f"WHERE memory_id IN ({','.join('?' * len(consolidated_ids))})",
                        consolidated_ids,
                    )
                    conn.commit()
                stats["episodes_consolidated"] = len(consolidated_ids)
        except Exception:  # noqa: BLE001 — consolidation must never break the agent
            return stats

        if prune_after:
            self._decay_importance()
            self.prune_memories()
        return stats

    @staticmethod
    def _extract_facts(
        user_message: str,
        assistant_response: str,
        context_summary: str,
        key_facts: list[str],
        extractor: Any | None,
    ) -> list[str]:
        """Produce candidate durable facts from a single episode."""
        if extractor is not None:
            try:
                extracted = extractor(user_message, assistant_response, context_summary)
                if extracted:
                    return [str(f) for f in extracted if str(f).strip()]
            except Exception:  # noqa: BLE001 — fall back to heuristic
                pass

        facts: list[str] = []
        seen: set[str] = set()
        for candidate in list(key_facts or []) + _split_fact_candidates(assistant_response):
            candidate = (candidate or "").strip()
            if not candidate or candidate in seen:
                continue
            # Only keep reasonably self-contained statements (short Chinese
            # facts such as drug names / contraindications are still valuable).
            if 3 <= len(candidate) <= 500:
                seen.add(candidate)
                facts.append(candidate)
        return facts

    def get_memory_stats(self) -> dict:
        """获取记忆系统统计信息。"""
        try:
            with self._connect() as conn:
                fact_count = conn.execute("SELECT COUNT(*) FROM memory_long_term").fetchone()[0]
                episode_count = conn.execute("SELECT COUNT(*) FROM memory_episodic").fetchone()[0]
                avg_importance = (
                    conn.execute("SELECT AVG(importance) FROM memory_long_term").fetchone()[0]
                    or 0.0
                )
                oldest_fact = conn.execute(
                    "SELECT MIN(created_at) FROM memory_long_term"
                ).fetchone()[0]
            return {
                "total_facts": fact_count,
                "total_episodes": episode_count,
                "avg_importance": round(avg_importance, 3),
                "oldest_fact": oldest_fact,
                "max_facts": self._max_facts,
                "max_episodes": self._max_episodes,
                "fact_ttl_days": self._fact_ttl_days,
                "episode_ttl_days": self._episode_ttl_days,
            }
        except Exception:
            return {"error": "stats unavailable"}


# ---------------------------------------------------------------------------
# Context Engineering
# ---------------------------------------------------------------------------


class ContextEngineer:
    """Assemble context windows with token budget management.

    Implements the art and science of filling the context window with
    just the right information for the next step.
    """

    # System prompt templates
    SYSTEM_PROMPT_BASE = """你是 DoctorAgent 的 AI 助手，专门帮助用户管理他们加密保险库中的文档。

你的能力：
1. 搜索和检索保险库中的文档
2. 回答关于文档内容的问题
3. 总结文档信息
4. 提供文档管理建议

重要规则：
- 仅基于提供的上下文回答问题
- 如果上下文中没有足够信息，请明确说明
- 引用来源时使用 [来源 X] 格式
- 保持回答简洁、准确
- 保护用户隐私，不要泄露敏感信息
"""

    SYSTEM_PROMPT_WITH_MEMORY = """你是 DoctorAgent 的 AI 努力，专门帮助用户管理他们加密保险库中的文档。

{memory_context}

你的能力：
1. 搜索和检索保险库中的文档
2. 回答关于文档内容的问题
3. 总结文档信息
4. 提供文档管理建议
5. 记住用户的偏好和历史交互

重要规则：
- 仅基于提供的上下文回答问题
- 如果上下文中没有足够信息，请明确说明
- 引用来源时使用 [来源 X] 格式
- 保持回答简洁、准确
- 保护用户隐私，不要泄露敏感信息
- 利用记忆提供个性化的回答
"""

    USER_PROMPT_TEMPLATE = """问题：{question}

请基于上述上下文信息回答问题。如果引用了特定来源，请使用 [来源 X] 格式标注。"""

    def __init__(
        self,
        memory_system: MemorySystem | None = None,
        embedding_provider: Any | None = None,
        llm_provider: Any | None = None,
        config: RagConfig | None = None,
    ) -> None:
        self.memory_system = memory_system
        # Providers power the optional context-engineering upgrades:
        #  * embedding_provider -> semantic de-duplication of near-duplicate chunks
        #  * llm_provider       -> LLM-based context compression when over budget
        # Both default to ``None`` so existing callers see no behaviour change.
        self.embedding_provider = embedding_provider
        self.llm_provider = llm_provider
        self.config = config if config is not None else RagConfig()

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text.

        Uses tiktoken for precise counting when available, falling back to
        the ``len(text) // 4`` heuristic via :func:`_count_tokens`.
        """
        return _count_tokens(text)

    def build_context(
        self,
        question: str,
        retrieved_chunks: list[dict[str, Any]],
        session_id: str | None = None,
        include_memory: bool = True,
        token_budget: int = MAX_CONTEXT_TOKENS,
    ) -> ContextWindow:
        """Build optimized context window.

        Implements context engineering principles:
        1. Prioritize most relevant information
        2. Compress when over budget
        3. Order context for maximum effectiveness
        """
        context = ContextWindow(user_query=question)

        # Start with system prompt
        memory_context = ""
        if include_memory and self.memory_system and session_id:
            memory_context = self._build_memory_context(session_id, question)

        if memory_context:
            context.system_prompt = self.SYSTEM_PROMPT_WITH_MEMORY.format(
                memory_context=memory_context
            )
            context.memory_used = True
        else:
            context.system_prompt = self.SYSTEM_PROMPT_BASE

        system_tokens = self.estimate_tokens(context.system_prompt)
        remaining_budget = token_budget - system_tokens

        # Context engineering upgrades (all opt-in, default off):
        # 1. Importance weighting re-orders chunks by source priority.
        # 2. Semantic de-dup drops near-duplicate chunks.
        engineered_chunks = retrieved_chunks
        if engineered_chunks:
            if self.config.enable_importance_weighting:
                engineered_chunks = self._apply_importance_weighting(engineered_chunks)
            if self.config.enable_semantic_dedup:
                engineered_chunks = self._semantic_dedup(engineered_chunks)

        # Add retrieved context
        if engineered_chunks:
            context.retrieved_context, context.sources = self._build_retrieved_context(
                engineered_chunks, remaining_budget
            )
            # 3. Optional LLM compression when the assembled context is large.
            if self.config.enable_context_compression and self.llm_provider:
                context.retrieved_context = self._compress_context(
                    context.retrieved_context, question, remaining_budget
                )
            remaining_budget -= self.estimate_tokens(context.retrieved_context)

        # Add conversation history
        if session_id and self.memory_system:
            history = self.memory_system.get_conversation_history(session_id)
            if history.turns:
                context.conversation_history = self._build_history_context(
                    history, remaining_budget
                )
                context.conversation_turns = len(history.turns)
                remaining_budget -= self.estimate_tokens(context.conversation_history)

        context.total_tokens = token_budget - remaining_budget
        return context

    def _build_memory_context(self, session_id: str, question: str) -> str:
        """Build memory context from long-term and episodic memory."""
        if not self.memory_system:
            return ""

        parts = []

        # Get relevant facts
        facts = self.memory_system.recall_facts(question, limit=3)
        if facts:
            facts_text = "\n".join(f"- {f.content}" for f in facts)
            parts.append(f"用户相关记忆：\n{facts_text}")

        # Get relevant episodes
        episodes = self.memory_system.recall_episodes(question, limit=2)
        if episodes:
            episodes_text = "\n".join(f"- 用户曾问：{e['user_message'][:100]}..." for e in episodes)
            parts.append(f"历史交互：\n{episodes_text}")

        return "\n\n".join(parts) if parts else ""

    def _build_retrieved_context(
        self,
        chunks: list[dict[str, Any]],
        budget: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Build retrieved context with source citations."""
        parts = []
        sources = []
        tokens_used = 0

        for i, chunk in enumerate(chunks, 1):
            source = chunk.get("vault_path", "unknown")
            category = chunk.get("category", "")
            text = chunk.get("text", "")

            chunk_text = f"[来源 {i}] (文件: {source}, 分类: {category})\n{text}"
            chunk_tokens = self.estimate_tokens(chunk_text)

            if tokens_used + chunk_tokens > budget:
                # Truncate this chunk to fit
                remaining_tokens = budget - tokens_used
                if remaining_tokens > 100:
                    truncated_text = text[: remaining_tokens * 4]
                    chunk_text = (
                        f"[来源 {i}] (文件: {source}, 分类: {category})\n{truncated_text}..."
                    )
                    parts.append(chunk_text)
                    sources.append({"source": source, "category": category, "truncated": True})
                break

            parts.append(chunk_text)
            sources.append({"source": source, "category": category, "truncated": False})
            tokens_used += chunk_tokens

        return "\n\n".join(parts), sources

    def _build_history_context(self, history: ConversationMemory, budget: int) -> str:
        """Build conversation history context."""
        if not history.turns:
            return ""

        parts = []
        tokens_used = 0

        # Add summary if available
        if history.summary:
            summary_text = f"对话摘要：{history.summary}"
            parts.append(summary_text)
            tokens_used += self.estimate_tokens(summary_text)

        # Add recent turns
        for turn in reversed(history.turns[-MAX_HISTORY_TURNS:]):
            turn_text = f"{turn.role}: {turn.content}"
            turn_tokens = self.estimate_tokens(turn_text)

            if tokens_used + turn_tokens > budget:
                break

            parts.append(turn_text)
            tokens_used += turn_tokens

        return "\n".join(reversed(parts))  # Reverse to get chronological order

    # ------------------------------------------------------------------
    # Context engineering upgrades (semantic dedup / compression / weighting)
    # ------------------------------------------------------------------

    def _apply_importance_weighting(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Re-order chunks by an importance-weighted score.

        Priority of evidence (highest first):
          1. User-marked importance (``chunk["importance"]`` / metadata flag)
          2. Recency of the source (``last_accessed`` / ``created_at``)
          3. Retrieval score (``chunk["score"]``)

        The weighted blend keeps high-recall but low-relevance hits from
        drowning out explicitly-important or freshly-accessed context.
        """
        now = datetime.now(UTC).timestamp()

        def _weight(chunk: dict[str, Any]) -> float:
            retrieval_score = float(chunk.get("score", 0.0))
            # User-marked importance: 0.0 - 1.0, defaults to 0.5.
            marked = float(
                chunk.get("importance", chunk.get("metadata", {}).get("importance", 0.5))
            )
            marked = max(0.0, min(1.0, marked))
            # Recency: normalised age in [0, 1] (1 = just now, 0 = very old).
            ts = chunk.get("last_accessed") or chunk.get("created_at") or ""
            recency = 0.5
            if ts:
                try:
                    recency_ts = datetime.fromisoformat(ts).timestamp()
                    age = max(0.0, now - recency_ts)
                    # Half-life of ~7 days.
                    recency = math.exp(-age / (7 * 24 * 3600))
                except (ValueError, TypeError):
                    recency = 0.5
            # Weights: marked 0.5, recency 0.2, retrieval 0.3.
            return 0.5 * marked + 0.2 * recency + 0.3 * retrieval_score

        return sorted(chunks, key=_weight, reverse=True)

    def _semantic_dedup(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop near-duplicate chunks by embedding cosine similarity.

        Greedy keep-first: a chunk is discarded when its embedding is more
        similar than ``config.semantic_dedup_threshold`` to any already-kept
        chunk. Falls back to keeping all chunks when no embedding provider is
        available or when embeddings cannot be computed.
        """
        if not chunks or len(chunks) == 1:
            return list(chunks)
        if self.embedding_provider is None:
            return list(chunks)

        texts = [c.get("text", "") for c in chunks]
        try:
            embeddings = self.embedding_provider.embed(texts)
        except Exception:  # noqa: BLE001 — never let dedup break the pipeline
            return list(chunks)
        if len(embeddings) != len(chunks):
            return list(chunks)

        threshold = self.config.semantic_dedup_threshold
        kept: list[dict[str, Any]] = []
        kept_vecs: list[list[float]] = []
        for chunk, vec in zip(chunks, embeddings, strict=True):
            is_dup = False
            for kv in kept_vecs:
                if _cosine_similarity(vec, kv) >= threshold:
                    is_dup = True
                    break
            if not is_dup:
                kept.append(chunk)
                kept_vecs.append(vec)
        return kept

    def _compress_context(self, context_text: str, question: str, budget: int) -> str:
        """Compress over-budget context into a faithful LLM summary.

        Only triggers when the assembled retrieved context exceeds
        :data:`COMPRESSION_TRIGGER_TOKENS`; otherwise the text is returned
        unchanged. The LLM is instructed to preserve facts relevant to the
        question. On any failure the original context is returned so the
        pipeline degrades safely.
        """
        if not context_text:
            return context_text
        if self.estimate_tokens(context_text) <= COMPRESSION_TRIGGER_TOKENS:
            return context_text
        if self.llm_provider is None:
            return context_text

        prompt = (
            "你是一个上下文压缩器。请将下面的检索上下文压缩为更短的版本，"
            "保留与问题相关的所有关键事实、数字和来源标注，去除冗余信息。\n\n"
            f"问题：{question}\n\n"
            f"待压缩上下文：\n{context_text}\n\n"
            "压缩后的上下文："
        )
        try:
            messages = [{"role": "user", "content": prompt}]
            compressed = self.llm_provider.chat_completion_sync(messages)
            if compressed and self.estimate_tokens(compressed) < self.estimate_tokens(context_text):
                return compressed.strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("Context compression failed, using original: %s", e)
        return context_text


# ---------------------------------------------------------------------------
# Query Transformer
# ---------------------------------------------------------------------------


class QueryTransformer:
    """Transform queries for better retrieval.

    Implements:
    - Query rewriting for clarity
    - Query expansion for better recall
    - Query decomposition for complex questions
    - HyDE (Hypothetical Document Embeddings)
    - Step-Back prompting
    - Multi-Query fusion with RRF
    """

    def __init__(self, llm_provider: Any | None = None) -> None:
        self.llm_provider = llm_provider

    def rewrite_query(self, query: str, conversation_history: str = "") -> str:
        """Rewrite query for better retrieval.

        Example:
            Input: "那个东西"
            Output: "用户之前提到的文件"
        """
        if not self.llm_provider:
            return query

        prompt = f"""请将以下用户问题重写为更清晰、更适合搜索的形式。
保持原意，但使问题更具体、更明确。

{f"对话历史：{conversation_history}" if conversation_history else ""}

原始问题：{query}

重写后的问题："""

        try:
            messages = [{"role": "user", "content": prompt}]
            response = self.llm_provider.chat_completion_sync(messages)
            return response.strip() if response else query
        except Exception:
            return query

    def expand_query(self, query: str) -> list[str]:
        """Expand query into multiple variations for better recall.

        Returns:
            List of query variations including the original
        """
        variations = [query]

        if not self.llm_provider:
            return variations

        prompt = f"""请将以下问题扩展为3个不同的搜索查询变体，用于在文档库中搜索相关信息。

原始问题：{query}

请以JSON数组格式返回，例如：
["变体1", "变体2", "变体3"]"""

        try:
            messages = [{"role": "user", "content": prompt}]
            response = self.llm_provider.chat_completion_sync(messages)
            if response:
                # Parse JSON array
                import json

                expanded = json.loads(response)
                if isinstance(expanded, list):
                    variations.extend(expanded[:3])
        except Exception:
            pass

        return variations

    def decompose_query(self, query: str) -> list[str]:
        """Decompose complex queries into sub-questions.

        Example:
            Input: "总结所有合同的关键条款和到期日期"
            Output: ["合同有哪些关键条款？", "合同的到期日期是什么？"]
        """
        if not self.llm_provider:
            return [query]

        prompt = f"""请将以下复杂问题分解为2-3个简单的子问题。

原始问题：{query}

请以JSON数组格式返回子问题列表。"""

        try:
            messages = [{"role": "user", "content": prompt}]
            response = self.llm_provider.chat_completion_sync(messages)
            if response:
                import json

                sub_questions = json.loads(response)
                if isinstance(sub_questions, list) and sub_questions:
                    return sub_questions[:3]
        except Exception:
            pass

        return [query]

    # ------------------------------------------------------------------
    # Advanced query transformations (HyDE / Step-Back / Multi-Query)
    # ------------------------------------------------------------------

    def hyde_transform(self, query: str, n: int = HYDE_DOC_COUNT) -> list[str]:
        """HyDE: generate hypothetical answer documents and use them to retrieve.

        Instead of embedding the raw query, HyDE asks the LLM to *answer* the
        question hypothetically, then embeds that answer document. The
        hypothetical doc is semantically closer to the target corpus passages
        than the terse query, which materially improves dense recall for
        factoid / open-ended questions.

        Returns a list of *n* hypothetical documents. When no LLM is available
        the original query is returned unchanged (so retrieval degrades to the
        legacy direct-query path).
        """
        if not self.llm_provider:
            return [query]

        prompt = (
            "请直接给出下面问题的假设性答案文档（一段连贯的回答文本，不要解释，"
            "不要注明这是假设）。答案应包含问题可能涉及的关键事实和术语，"
            "即便信息不完整也要给出最合理的假设。\n\n"
            f"问题：{query}\n\n"
            "假设性答案文档："
        )
        docs: list[str] = []
        for _ in range(max(1, n)):
            try:
                messages = [{"role": "user", "content": prompt}]
                response = self.llm_provider.chat_completion_sync(messages)
                if response and response.strip():
                    docs.append(response.strip())
            except Exception:  # noqa: BLE001
                continue
        return docs if docs else [query]

    def step_back_query(self, query: str) -> str | None:
        """Step-Back prompting: derive a more abstract / generic question.

        For overly-specific questions, a broader "step-back" question retrieves
        the surrounding concepts that ground the specific answer. The caller
        retrieves for *both* the original and the step-back query and merges.

        Returns the abstract question, or ``None`` when no LLM is available or
        generation fails (caller then retrieves only the original query).
        """
        if not self.llm_provider:
            return None

        prompt = (
            "你是一个问题抽象专家。请将下面这个过于具体的问题，"
            "改写为一个更宏观、更抽象的“后退一步”问题，使其能检索到"
            "回答原问题所需的背景概念与原理。只输出改写后的问题，不要解释。\n\n"
            f"原始问题：{query}\n\n"
            "后退一步的问题："
        )
        try:
            messages = [{"role": "user", "content": prompt}]
            response = self.llm_provider.chat_completion_sync(messages)
            if response and response.strip():
                return response.strip()
        except Exception:  # noqa: BLE001
            return None
        return None

    def multi_query(self, query: str, n: int = MULTI_QUERY_COUNT) -> list[str]:
        """Generate *n* diverse query rewrites for multi-query fusion.

        Unlike :meth:`expand_query` (which only augments recall), the rewrites
        here are intended to be retrieved *independently* and then fused with
        :meth:`rrf_fuse`. The original query is always the first element so it
        is never lost. Returns ``[query]`` when no LLM is available.
        """
        variations = [query]
        if not self.llm_provider:
            return variations

        prompt = (
            f"请将下面的问题改写为{n}个不同角度的搜索查询，用于检索文档库。"
            "每个变体应覆盖不同的表述或侧面，但保持同一意图。\n\n"
            f"原始问题：{query}\n\n"
            '请以JSON数组格式返回，例如：["变体1", "变体2", "变体3"]'
        )
        try:
            messages = [{"role": "user", "content": prompt}]
            response = self.llm_provider.chat_completion_sync(messages)
            if response:
                rewrites = json.loads(response)
                if isinstance(rewrites, list):
                    for r in rewrites[:n]:
                        if isinstance(r, str) and r.strip() and r.strip() not in variations:
                            variations.append(r.strip())
        except Exception:  # noqa: BLE001
            pass
        return variations

    @staticmethod
    def rrf_fuse(
        ranked_lists: list[list[dict[str, Any]]],
        k: int = 60,
        weights: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion of multiple ranked chunk lists.

        Each input list is a ranking of chunk dicts (each carrying a
        ``chunk_id``). A chunk's fused score is the weighted sum of
        ``1 / (k + rank)`` across all lists it appears in. The first list's
        chunk objects are preferred when the same ``chunk_id`` appears in
        multiple lists (so metadata/scores from the primary retrieval are
        preserved), updated with the fused score.

        This is the core of multi-query fusion: several rewrites each produce a
        ranking whose raw scores are incomparable, so RRF merges them by rank.
        """
        if not ranked_lists:
            return []
        # Drop empty lists.
        ranked_lists = [rl for rl in ranked_lists if rl]
        if not ranked_lists:
            return []
        if len(ranked_lists) == 1:
            return list(ranked_lists[0])

        if weights is None:
            weights = [1.0] * len(ranked_lists)
        if len(weights) != len(ranked_lists):
            weights = [1.0] * len(ranked_lists)

        rrf_scores: dict[str, float] = defaultdict(float)
        chunk_map: dict[str, dict[str, Any]] = {}
        for weight, ranked in zip(weights, ranked_lists, strict=True):
            for rank, chunk in enumerate(ranked):
                cid = chunk.get("chunk_id", "")
                if not cid:
                    continue
                rrf_scores[cid] += weight * (1.0 / (k + rank))
                # Prefer the first occurrence's chunk object.
                if cid not in chunk_map:
                    chunk_map[cid] = dict(chunk)

        sorted_ids = sorted(rrf_scores, key=lambda c: rrf_scores[c], reverse=True)
        return [{**chunk_map[cid], "score": rrf_scores[cid]} for cid in sorted_ids]


# ---------------------------------------------------------------------------
# Semantic Chunker
# ---------------------------------------------------------------------------


class SemanticChunker:
    """Recursive character text splitter with semantic awareness."""

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._separators = [
            "\n\n",
            "\n",
            "。",
            ".",
            "！",
            "!",
            "？",
            "?",
            "；",
            ";",
            "，",
            ",",
            " ",
            "",
        ]

    def chunk_text(self, text: str) -> list[dict[str, Any]]:
        """Split text into semantic chunks."""
        if not text or not text.strip():
            return []

        chunks: list[dict[str, Any]] = []
        self._recursive_split(text, 0, chunks)
        return self._merge_small_chunks(chunks)

    def _recursive_split(self, text: str, start_offset: int, result: list[dict[str, Any]]) -> None:
        """Recursively split text."""
        if len(text) <= self.chunk_size:
            if text.strip():
                result.append(
                    {
                        "text": text,
                        "start_char": start_offset,
                        "end_char": start_offset + len(text),
                    }
                )
            return

        for separator in self._separators:
            if not separator:
                split_point = self.chunk_size
                left = text[:split_point]
                right_start = split_point - self.chunk_overlap
                if right_start <= 0:
                    right_start = split_point
                right = text[right_start:]
                if left.strip():
                    result.append(
                        {
                            "text": left,
                            "start_char": start_offset,
                            "end_char": start_offset + split_point,
                        }
                    )
                if right.strip():
                    self._recursive_split(
                        right, start_offset + right_start, result
                    )
                return

            idx = text.rfind(separator, 0, self.chunk_size)
            if idx > 0:
                split_point = idx + len(separator)
                left = text[:split_point]
                right_start = split_point - self.chunk_overlap
                if right_start <= 0:
                    right_start = split_point
                right = text[right_start:]
                if left.strip():
                    result.append(
                        {
                            "text": left,
                            "start_char": start_offset,
                            "end_char": start_offset + split_point,
                        }
                    )
                if right.strip():
                    self._recursive_split(
                        right, start_offset + right_start, result
                    )
                return

        split_point = self.chunk_size
        left = text[:split_point]
        right_start = split_point - self.chunk_overlap
        if right_start <= 0:
            right_start = split_point
        right = text[right_start:]
        if left.strip():
            result.append(
                {
                    "text": left,
                    "start_char": start_offset,
                    "end_char": start_offset + split_point,
                }
            )
        if right.strip():
            self._recursive_split(right, start_offset + right_start, result)

    def _merge_small_chunks(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge small chunks."""
        if not chunks:
            return []

        merged: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None

        for chunk in chunks:
            if current is None:
                current = chunk.copy()
            elif len(current["text"]) + len(chunk["text"]) <= self.chunk_size:
                current["text"] += chunk["text"]
                current["end_char"] = chunk["end_char"]
            else:
                merged.append(current)
                current = chunk.copy()

        if current is not None:
            merged.append(current)

        return merged


# ---------------------------------------------------------------------------
# BM25 Search
# ---------------------------------------------------------------------------


class BM25Search:
    """BM25 search using SQLite FTS5."""

    def __init__(self, db_path: Path, tenant_id: str = "default") -> None:
        self.db_path = db_path
        self.tenant_id = tenant_id

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection."""
        return open_sqlite(self.db_path)

    def search(self, query: str, top_k: int = INITIAL_RETRIEVAL_K) -> list[dict[str, Any]]:
        """BM25 search on chunks."""
        # jieba-segment the query so CJK terms match the segmented FTS index.
        from doctoragent._utils import tokenize_for_fts

        tokens = [t for t in tokenize_for_fts(query).split() if t]
        if not tokens:
            return []

        quote = '"'
        escaped_quote = '""'
        # FTS5 implicit AND is too strict for segmented CJK queries (a
        # document rarely contains every query word). Use OR so any matching
        # term is a hit; BM25 ranking (ORDER BY rank) surfaces docs that
        # match more / rarer terms first.
        match_expr = " OR ".join(
            f"{quote}{token.replace(quote, escaped_quote)}{quote}*" for token in tokens
        )

        with self._connect() as conn:
            try:
                # MATCH against the segmented FTS index, but return the RAW
                # chunk text from vault_chunks (the FTS `text` column stores
                # jieba-segmented words with spaces, which is fine for
                # matching but not for display / LLM context).
                rows = conn.execute(
                    """
                    SELECT fts.chunk_id, fts.task_id, fts.vault_path,
                           fts.category, fts.summary, chunks.text, fts.rank
                    FROM vault_chunks_fts fts
                    LEFT JOIN vault_chunks chunks ON chunks.chunk_id = fts.chunk_id
                    WHERE vault_chunks_fts MATCH ? AND fts.tenant_id = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match_expr, self.tenant_id, top_k),
                ).fetchall()
            except sqlite3.Error:
                return []

        return [
            {
                "chunk_id": row[0],
                "task_id": row[1],
                "vault_path": row[2],
                "category": row[3],
                "summary": row[4],
                "text": row[5] or "",
                "score": 1.0 / (1.0 + abs(float(row[6]))),
            }
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Approximate Nearest-Neighbour (ANN) Indexes
# ---------------------------------------------------------------------------


class AnnIndex:
    """Abstract ANN index interface.

    Two concrete implementations are provided:

    * :class:`NumpyAnnIndex` — exact but *vectorised* brute-force search using
      a single matrix-vector product. This is the "numpy argsort optimisation"
      and is dramatically faster than the previous Python loop while remaining
      exact; it is the default for moderately-sized corpora.
    * :class:`LshAnnIndex` — random-projection Locality-Sensitive Hashing for
      sub-linear approximate search on very large corpora.

    Both avoid any third-party dependency beyond NumPy (already required).
    """

    def build(self, vectors: list[list[float]], ids: list[str]) -> None:
        raise NotImplementedError

    def search(self, query: list[float], top_k: int) -> list[tuple[str, float]]:
        """Return ``[(id, score), ...]`` sorted by descending similarity."""
        raise NotImplementedError

    @property
    def size(self) -> int:
        return 0

    def save(self, path: Path) -> None:  # noqa: B027
        """Persist the index to *path* (no-op by default)."""

    @classmethod
    def load(cls, path: Path) -> AnnIndex | None:  # noqa: B027
        """Load an index from *path*, or ``None`` if unavailable."""
        return None


class NumpyAnnIndex(AnnIndex):
    """Vectorised exact nearest-neighbour search over a NumPy matrix.

    Replaces the previous O(n) Python-loop scan with one ``matrix @ query``
    product followed by an ``argsort``. Exact (no recall loss) yet fast enough
    for tens of thousands of vectors, which covers the DoctorAgent vault scale.
    """

    def __init__(self) -> None:
        self._matrix: Any = None  # np.ndarray (n, d)
        self._ids: list[str] = []
        self._dim: int = 0

    def build(self, vectors: list[list[float]], ids: list[str]) -> None:
        import numpy as np

        if not vectors:
            self._matrix = None
            self._ids = []
            self._dim = 0
            return
        self._matrix = np.asarray(vectors, dtype=np.float32)
        if self._matrix.ndim == 1:
            self._matrix = self._matrix.reshape(1, -1)
        self._ids = list(ids)
        self._dim = int(self._matrix.shape[1])

    @property
    def size(self) -> int:
        return len(self._ids)

    def search(self, query: list[float], top_k: int) -> list[tuple[str, float]]:
        if self._matrix is None or not self._ids:
            return []
        scores = _cosine_similarity_matrix(query, self._matrix)
        if not scores:
            return []
        # Partial selection: argpartition for top_k, then sort that subset.
        n = len(scores)
        k = min(top_k, n)
        import numpy as np

        idx = np.argpartition(np.asarray(scores), n - k)[n - k :]
        # Sort the candidate subset by score descending.
        idx = idx[np.argsort(np.asarray(scores)[idx])[::-1]]
        return [(self._ids[i], float(scores[i])) for i in idx[:k]]

    def save(self, path: Path) -> None:
        if self._matrix is None:
            return
        import numpy as np

        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(str(path) + ".npy", self._matrix)
        meta = {"ids": self._ids, "dim": self._dim}
        Path(str(path) + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> NumpyAnnIndex | None:
        import numpy as np

        npy = Path(str(path) + ".npy")
        meta_path = Path(str(path) + ".meta.json")
        if not npy.exists() or not meta_path.exists():
            return None
        try:
            matrix = np.load(str(npy))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        idx = cls()
        idx._matrix = matrix
        idx._ids = list(meta.get("ids", []))
        idx._dim = int(meta.get("dim", 0))
        if matrix.ndim == 1 and idx._dim:
            matrix = matrix.reshape(1, -1)
        idx._matrix = matrix
        return idx


class LshAnnIndex(AnnIndex):
    """Random-projection Locality-Sensitive Hashing (cosine similarity).

    Generates ``num_hashes`` random hyperplanes; each vector is hashed to a
    bit-signature. Vectors sharing the same (or a nearby) signature land in the
    same bucket, so a query only scans a small candidate set instead of the
    full corpus. Candidates are then re-ranked by exact cosine similarity.

    This is the genuinely *approximate* path, used for very large corpora to
    keep retrieval sub-linear. A hamming-distance-1 probe plus a fallback to
    full scan when too few candidates are found keeps recall high.
    """

    def __init__(self, num_hashes: int = 16, seed: int = 0) -> None:
        self.num_hashes = num_hashes
        self.seed = seed
        self._planes: Any = None  # np.ndarray (num_hashes, d)
        self._buckets: dict[str, list[int]] = defaultdict(list)
        self._vectors: list[list[float]] = []
        self._ids: list[str] = []
        self._dim: int = 0

    def _signature(self, vec: Any) -> str:
        import numpy as np

        v = np.asarray(vec, dtype=np.float32)
        if self._planes is None or v.shape[0] != self._planes.shape[1]:
            return ""
        proj = self._planes @ v
        bits = (proj > 0).astype(np.uint8)
        return "".join(str(b) for b in bits)

    def build(self, vectors: list[list[float]], ids: list[str]) -> None:
        import numpy as np

        if not vectors:
            self._vectors = []
            self._ids = []
            self._planes = None
            self._buckets = defaultdict(list)
            self._dim = 0
            return
        self._vectors = [list(v) for v in vectors]
        self._ids = list(ids)
        self._dim = len(vectors[0])
        rng = np.random.default_rng(self.seed)
        self._planes = rng.standard_normal((self.num_hashes, self._dim)).astype(np.float32)
        self._buckets = defaultdict(list)
        for i, vec in enumerate(self._vectors):
            sig = self._signature(vec)
            self._buckets[sig].append(i)

    @property
    def size(self) -> int:
        return len(self._ids)

    def search(self, query: list[float], top_k: int) -> list[tuple[str, float]]:
        if not self._ids or self._planes is None:
            return []
        sig = self._signature(query)
        if not sig:
            return []
        # Probe exact bucket + hamming-distance-1 buckets.
        candidates: set[int] = set(self._buckets.get(sig, []))
        if len(candidates) < top_k:
            for i in range(len(sig)):
                flipped = sig[:i] + ("1" if sig[i] == "0" else "0") + sig[i + 1 :]
                candidates.update(self._buckets.get(flipped, []))
        # Fallback: if still too sparse, scan everything (keeps recall safe).
        if len(candidates) < top_k:
            candidates = set(range(len(self._ids)))

        scored: list[tuple[str, float]] = []
        for i in candidates:
            score = _cosine_similarity(query, self._vectors[i])
            scored.append((self._ids[i], score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def save(self, path: Path) -> None:
        import numpy as np

        if self._planes is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(str(path) + ".lsh.npy", self._planes)
        meta = {
            "num_hashes": self.num_hashes,
            "seed": self.seed,
            "dim": self._dim,
            "ids": self._ids,
            "vectors": self._vectors,
            "buckets": dict(self._buckets),
        }
        Path(str(path) + ".lsh.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> LshAnnIndex | None:
        import numpy as np

        npy = Path(str(path) + ".lsh.npy")
        meta_path = Path(str(path) + ".lsh.meta.json")
        if not npy.exists() or not meta_path.exists():
            return None
        try:
            planes = np.load(str(npy))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        idx = cls(num_hashes=int(meta.get("num_hashes", 16)), seed=int(meta.get("seed", 0)))
        idx._planes = planes
        idx._dim = int(meta.get("dim", 0))
        idx._ids = list(meta.get("ids", []))
        idx._vectors = [list(v) for v in meta.get("vectors", [])]
        idx._buckets = defaultdict(list, {k: list(v) for k, v in meta.get("buckets", {}).items()})
        return idx


def build_ann_index(
    vectors: list[list[float]],
    ids: list[str],
    mode: str = "auto",
    threshold: int = ANN_VECTOR_THRESHOLD,
) -> AnnIndex | None:
    """Build an ANN index for the corpus.

    ``mode`` selects the index type:
      * ``"numpy"`` / ``"auto"`` -> :class:`NumpyAnnIndex` (exact, vectorised).
        This is the recommended default: it is exact (no recall loss) yet uses
        a single matrix-vector product instead of a Python loop.
      * ``"lsh"`` -> :class:`LshAnnIndex` (random-projection, approximate).
        Opt-in for very large corpora where sub-linear scan matters.
      * ``"brute"`` -> ``None`` (signal the caller to use its in-retriever loop).

    The *threshold* argument is retained for API symmetry; the decision of
    *when* to build an index (vs. keep the small-data brute-force loop) is made
    by :class:`HybridRetriever`, which only calls this helper once the corpus
    exceeds its configured threshold. Returns ``None`` for an empty corpus.
    """
    _ = threshold  # threshold decision is owned by the retriever
    if not vectors:
        return None

    if mode == "brute":
        return None
    if mode == "lsh":
        idx = LshAnnIndex()
        idx.build(vectors, ids)
        return idx
    # "numpy" or "auto" -> exact vectorised index.
    idx = NumpyAnnIndex()
    idx.build(vectors, ids)
    return idx


# ---------------------------------------------------------------------------
# Hybrid Retriever
# ---------------------------------------------------------------------------


class HybridRetriever:
    """Hybrid retrieval combining BM25 and Dense with RRF.

    Advanced upgrades (all opt-in via :class:`RagConfig`):

    * **ANN index** — when the chunk-vector count exceeds ``config.ann_threshold``
      a NumPy-backed index (exact vectorised by default, optional LSH) replaces
      the O(n) Python-loop scan; below the threshold the brute-force loop is
      kept (faster for small corpora). The index is cached and optionally
      persisted to disk to avoid rebuilding on every query.
    * **Recursive retrieval** — a two-level search that first matches
      per-document summary embeddings and then drills into the matched
      documents' chunks (``enable_recursive_retrieval``).
    * **Parent-child expansion** — retrieve precise small chunks, then expand
      each to its parent chunk for richer context (``enable_parent_child``).
    """

    def __init__(
        self,
        db_path: Path,
        embedding_provider: Any | None = None,
        tenant_id: str = "default",
        config: RagConfig | None = None,
        task_store: Any | None = None,
    ) -> None:
        self.db_path = db_path
        self.embedding_provider = embedding_provider
        self.tenant_id = tenant_id
        self.bm25 = BM25Search(db_path, tenant_id)
        self.config = config if config is not None else RagConfig()
        # Optional TaskStore reference used for parent-chunk resolution. When
        # absent, parent lookup falls back to a direct SQL query on the same DB.
        self.task_store = task_store
        # ANN index cache + staleness tracking.
        self._ann_index: AnnIndex | None = None
        self._ann_signature: int = -1  # number of vectors the index was built for
        self._ann_index_lock = threading.Lock()
        # P2 scalability hook: when an alternative vector store backend is
        # requested we surface a warning so operators know the retriever is
        # still using the SQLite dense path; full wiring is a follow-up.
        # The actual store is *not* constructed here to keep behaviour
        # identical for existing callers (no new dependencies at runtime).
        if self.config.vector_backend.lower() != "sqlite":
            logger.warning(
                "RagConfig.vector_backend=%r requested but HybridRetriever "
                "still uses the inline SQLite dense path; integrate "
                "doctoragent.model.vectorstore.create_vector_store to switch "
                "backends. path=%r",
                self.config.vector_backend,
                self.config.vector_backend_path,
            )

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection."""
        return open_sqlite(self.db_path)

    # ------------------------------------------------------------------
    # ANN index management
    # ------------------------------------------------------------------

    @property
    def _ann_index_path(self) -> Path:
        """Sidecar path for index persistence (next to the SQLite DB)."""
        return Path(str(self.db_path) + ".ann")

    def _load_all_chunk_vectors(self) -> list[tuple[str, list[float]]]:
        """Load every ``(chunk_id, embedding)`` pair for the current tenant.

        Used both to build the ANN index and as the brute-force fallback. Rows
        whose embedding BLOB is missing or corrupt are skipped. Returns an
        empty list when the ``vault_chunks`` table does not exist yet.
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT chunk_id, embedding FROM vault_chunks WHERE tenant_id = ?",
                    (self.tenant_id,),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[tuple[str, list[float]]] = []
        for row in rows:
            vec = self._read_vector(row[1])
            if vec is not None:
                out.append((row[0], vec))
        return out

    def _get_ann_index(self) -> AnnIndex | None:
        """Return a fresh-or-cached ANN index, or ``None`` for brute force.

        The index is (re)built when the chunk-vector count has changed since
        the last build. When ``config.ann_persist`` is on, a built index is
        saved to disk and a stale/persisted index is loaded on first use.
        """
        if not self.config.enable_ann_index:
            return None

        vectors = self._load_all_chunk_vectors()
        n = len(vectors)
        # Below the threshold brute force is faster (and preserves legacy
        # behaviour exactly for the common small-corpus / test case).
        if n < self.config.ann_threshold:
            return None

        with self._ann_index_lock:
            if self._ann_index is not None and self._ann_signature == n:
                return self._ann_index

            # Try loading a persisted index matching the current vector count.
            if self.config.ann_persist and self._ann_index is None:
                loaded = self._load_persisted_index(n)
                if loaded is not None:
                    self._ann_index = loaded
                    self._ann_signature = n
                    return self._ann_index

            mode = self.config.ann_mode
            ids = [v[0] for v in vectors]
            vecs = [v[1] for v in vectors]
            index = build_ann_index(vecs, ids, mode=mode, threshold=self.config.ann_threshold)
            self._ann_index = index
            self._ann_signature = n
            if index is not None and self.config.ann_persist:
                try:
                    index.save(self._ann_index_path)
                except Exception as e:  # noqa: BLE001 — persistence is best-effort
                    logger.warning("ANN index persistence failed: %s", e)
            return index

    def _load_persisted_index(self, expected_size: int) -> AnnIndex | None:
        """Load a persisted index whose size matches *expected_size*."""
        base = self._ann_index_path
        mode = self.config.ann_mode
        candidates: list[AnnIndex | None] = []
        if mode in ("lsh",):
            candidates.append(LshAnnIndex.load(base))
        else:
            candidates.append(NumpyAnnIndex.load(base))
            candidates.append(LshAnnIndex.load(base))
        for idx in candidates:
            if idx is not None and idx.size == expected_size:
                logger.debug("Loaded persisted ANN index (size=%d)", expected_size)
                return idx
        return None

    def invalidate_index(self) -> None:
        """Drop the cached ANN index (call after chunks are added/removed)."""
        with self._ann_index_lock:
            self._ann_index = None
            self._ann_signature = -1

    def retrieve(
        self,
        query: str,
        top_k: int = FINAL_K,
        bm25_weight: float = 0.3,
        dense_weight: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Hybrid retrieval with RRF fusion.

        Applies parent-child context expansion at the end when
        ``config.enable_parent_child`` is on.
        """
        bm25_results = self.bm25.search(query, top_k=INITIAL_RETRIEVAL_K)

        dense_results: list[dict[str, Any]] = []
        if self.embedding_provider is not None:
            dense_results = self._dense_search(query, INITIAL_RETRIEVAL_K)

        if not bm25_results and not dense_results:
            return []

        if not bm25_results:
            return self.expand_to_parents(dense_results[:top_k])
        if not dense_results:
            return self.expand_to_parents(bm25_results[:top_k])

        # RRF fusion
        rrf_scores: dict[str, float] = defaultdict(float)
        chunk_map: dict[str, dict[str, Any]] = {}
        k = 60

        for rank, result in enumerate(bm25_results):
            chunk_id = result["chunk_id"]
            rrf_scores[chunk_id] += bm25_weight * (1.0 / (k + rank))
            chunk_map[chunk_id] = result

        for rank, result in enumerate(dense_results):
            chunk_id = result["chunk_id"]
            rrf_scores[chunk_id] += dense_weight * (1.0 / (k + rank))
            if chunk_id not in chunk_map:
                chunk_map[chunk_id] = result

        sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)

        fused = [{**chunk_map[cid], "score": rrf_scores[cid]} for cid in sorted_ids[:top_k]]
        return self.expand_to_parents(fused)

    def _dense_search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """Dense vector search.

        Uses the cached ANN index when the corpus exceeds the configured
        threshold (vectorised NumPy or LSH); otherwise falls back to the
        original brute-force Python loop. Both paths return identical-shape
        result dicts. Recursive retrieval (summary -> chunks) is applied when
        enabled.
        """
        if self.embedding_provider is None:
            return []

        try:
            query_embedding = self.embedding_provider.embed([query])[0]
        except Exception:
            return []

        # Recursive retrieval: narrow the chunk search to documents whose
        # summary embedding matches the query first.
        task_filter: set[str] | None = None
        if self.config.enable_recursive_retrieval:
            task_filter = self._recursive_doc_filter(query_embedding)

        rows = self._load_chunk_rows(task_filter)

        ann = self._get_ann_index()
        if ann is not None and task_filter is None:
            # Index covers the whole corpus; use it for the global search.
            hits = ann.search(query_embedding, top_k)
            return self._materialise_index_hits(hits, rows, top_k)

        # Brute-force path (small corpus, or recursive filter narrowed the set).
        scored: list[tuple[float, list[Any]]] = []
        for row in rows:
            vector = self._read_vector(row[6])
            if vector is None:
                continue
            score = self._cosine_similarity(query_embedding, vector)
            scored.append((score, list(row[:6])))

        scored.sort(key=lambda x: x[0], reverse=True)

        return [
            {
                "chunk_id": row[1][0],
                "task_id": row[1][1],
                "vault_path": row[1][2],
                "category": row[1][3],
                "summary": row[1][4],
                "text": row[1][5],
                "score": row[0],
            }
            for row in scored[:top_k]
        ]

    def _load_chunk_rows(self, task_filter: set[str] | None) -> list[list[Any]]:
        """Load chunk rows, optionally restricted to a set of task_ids.

        Returns an empty list when the ``vault_chunks`` table does not exist
        yet (e.g. the DB was just created and no chunks have been indexed),
        mirroring the graceful degradation of :class:`BM25Search`.
        """
        with self._connect() as conn:
            try:
                if task_filter is None:
                    rows = conn.execute(
                        "SELECT chunk_id, task_id, vault_path, category, summary, text, "
                        "embedding FROM vault_chunks WHERE tenant_id = ?",
                        (self.tenant_id,),
                    ).fetchall()
                elif not task_filter:
                    return []
                else:
                    placeholders = ",".join("?" * len(task_filter))
                    rows = conn.execute(
                        f"SELECT chunk_id, task_id, vault_path, category, summary, text, "  # nosec B608
                        f"embedding FROM vault_chunks WHERE tenant_id = ? "
                        f"AND task_id IN ({placeholders})",
                        (self.tenant_id, *task_filter),
                    ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [list(r) for r in rows]

    def _materialise_index_hits(
        self,
        hits: list[tuple[str, float]],
        rows: list[list[Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """Map ANN ``[(chunk_id, score)]`` hits back to full chunk dicts."""
        row_by_id: dict[str, list[Any]] = {r[0]: r for r in rows}
        results: list[dict[str, Any]] = []
        for chunk_id, score in hits:
            row = row_by_id.get(chunk_id)
            if row is None:
                continue
            results.append(
                {
                    "chunk_id": row[0],
                    "task_id": row[1],
                    "vault_path": row[2],
                    "category": row[3],
                    "summary": row[4],
                    "text": row[5],
                    "score": score,
                }
            )
            if len(results) >= top_k:
                break
        return results

    # ------------------------------------------------------------------
    # Recursive retrieval (document-summary index)
    # ------------------------------------------------------------------

    def _recursive_doc_filter(self, query_embedding: list[float]) -> set[str]:
        """Two-level retrieval: top documents by summary embedding.

        Reads per-document summary vectors from ``vault_vectors`` (populated by
        :class:`doctoragent.orchestration.task_store.TaskStore.index_embedding`),
        ranks them by cosine similarity to the query, and returns the
        ``task_id`` set of the top :data:`RECURSIVE_DOC_TOP_K` documents. The
        caller then restricts chunk search to those documents. Returns an empty
        set when no document vectors exist (so the dense search short-circuits
        to ``[]`` cleanly).
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT task_id, vector_blob FROM vault_vectors WHERE tenant_id = ?",
                    (self.tenant_id,),
                ).fetchall()
        except sqlite3.OperationalError:
            return set()

        scored: list[tuple[float, str]] = []
        for row in rows:
            vec = self._read_vector(row[1])
            if vec is None:
                continue
            score = self._cosine_similarity(query_embedding, vec)
            scored.append((score, row[0]))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_doc_count = min(RECURSIVE_DOC_TOP_K, len(scored))
        return {tid for _, tid in scored[:top_doc_count]}

    # ------------------------------------------------------------------
    # Parent-child chunk expansion
    # ------------------------------------------------------------------

    def expand_to_parents(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Expand each retrieved small chunk to its parent (wider context).

        For a chunk with a ``parent_chunk_id``, the parent's (longer) text
        replaces the child's text while the child's retrieval score is kept
        (the match was on the precise small chunk). Chunks without a parent are
        returned unchanged. Duplicated parents are de-duplicated (first wins).
        When no task_store is wired up, a direct SQL lookup is used.
        """
        if not self.config.enable_parent_child or not chunks:
            return chunks

        seen_parents: set[str] = set()
        expanded: list[dict[str, Any]] = []
        for chunk in chunks:
            parent = self._resolve_parent_chunk(chunk.get("chunk_id", ""))
            if parent is None:
                expanded.append(chunk)
                continue
            parent_id = parent.get("chunk_id", "")
            if parent_id and parent_id in seen_parents:
                continue
            if parent_id:
                seen_parents.add(parent_id)
            # Keep the child's score/rank but use the parent's wider text.
            merged = dict(chunk)
            merged["text"] = parent.get("text", chunk.get("text", ""))
            merged["parent_chunk_id"] = parent_id
            merged["child_chunk_id"] = chunk.get("chunk_id", "")
            expanded.append(merged)
        return expanded

    def _resolve_parent_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        """Resolve a chunk's parent via TaskStore (preferred) or direct SQL."""
        if not chunk_id:
            return None
        if self.task_store is not None:
            try:
                return self.task_store.get_parent_chunk(chunk_id)
            except Exception:  # noqa: BLE001
                return None
        # Direct SQL fallback on the same DB.
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT parent_chunk_id FROM vault_chunks WHERE chunk_id = ? AND tenant_id = ?",
                    (chunk_id, self.tenant_id),
                ).fetchone()
            if row is None or not row[0]:
                return None
            parent_id = row[0]
            with self._connect() as conn:
                prow = conn.execute(
                    "SELECT chunk_id, text FROM vault_chunks WHERE chunk_id = ? AND tenant_id = ?",
                    (parent_id, self.tenant_id),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        if prow is None:
            return None
        return {"chunk_id": prow[0], "text": prow[1]}

    @staticmethod
    def _read_vector(blob: Any) -> list[float] | None:
        """Read vector from BLOB."""
        if blob is None:
            return None
        try:
            return list(struct.unpack(f"{len(blob) // 8}d", blob))
        except (struct.error, MemoryError):
            return None

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity.

        Thin wrapper around :func:`doctoragent._utils.cosine_similarity` kept as a
        static method so existing call sites continue to work.
        """
        return _cosine_similarity(a, b)


# ---------------------------------------------------------------------------
# Re-ranker
# ---------------------------------------------------------------------------


class Reranker:
    """Cross-encoder re-ranker for precision improvement."""

    def __init__(self, model_name: str = CROSS_ENCODER_MODEL) -> None:
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _load_model(self) -> Any:
        """Lazy-load the cross-encoder model.

        Logs the (one-time) reason when the model cannot be loaded so that the
        previously-silent degradation to "no reranking" is now observable in
        production logs.
        """
        if self._model is not None:
            return self._model

        with self._lock:
            if self._model is not None:
                return self._model

            try:
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(self.model_name)
                logger.info("Cross-encoder reranker loaded: %s", self.model_name)
            except ImportError:
                self._model = None
                logger.info(
                    "Reranker disabled: sentence-transformers not installed "
                    "(model=%s). Re-ranking will be skipped.",
                    self.model_name,
                )
            except Exception as e:  # noqa: BLE001
                self._model = None
                logger.warning(
                    "Reranker disabled: failed to load cross-encoder %s: %s. "
                    "Re-ranking will be skipped.",
                    self.model_name,
                    e,
                )

        return self._model

    def rerank(
        self,
        query: str,
        chunks: list[dict[str, Any]],
        top_k: int = FINAL_K,
    ) -> list[dict[str, Any]]:
        """Re-rank chunks using cross-encoder.

        Degrades gracefully (returns chunks truncated to ``top_k``) when the
        cross-encoder model is unavailable, logging the fallback so the
        degradation is never silent.
        """
        if not chunks:
            return []

        model = self._load_model()
        if model is None:
            return chunks[:top_k]

        pairs = [(query, chunk.get("text", "")) for chunk in chunks]

        try:
            scores = model.predict(pairs)
            for chunk, score in zip(chunks, scores, strict=True):
                chunk["rerank_score"] = float(score)

            reranked = sorted(chunks, key=lambda x: x.get("rerank_score", 0), reverse=True)
            return reranked[:top_k]
        except Exception as e:  # noqa: BLE001
            logger.warning("Reranker prediction failed, falling back to retrieval order: %s", e)
            return chunks[:top_k]


# ---------------------------------------------------------------------------
# Response Synthesizer (Compact / Refine / Tree Summarize)
# ---------------------------------------------------------------------------


class ResponseSynthesizer:
    """Pluggable response-synthesis strategies over retrieved chunks.

    Three strategies are implemented, selectable by chunk count or explicitly:

    * **Compact** — concatenate all chunks into one context and generate a
      single answer. This is the legacy behaviour and is optimal for small
      context windows / few chunks.
    * **Refine** — generate an intermediate answer from the first chunk, then
      iteratively refine it with each subsequent chunk. Best for multi-source
      information that must be woven together.
    * **Tree Summarize** — recursively pair-merge chunk summaries into a tree,
      halving the working set each round until one answer remains. Best for a
      large number of chunks where pairwise reduction keeps each LLM call
      focused.

    All strategies degrade to the pipeline's fallback answer when no LLM is
    available.
    """

    COMPACT = "compact"
    REFINE = "refine"
    TREE = "tree"
    AUTO = "auto"

    def __init__(self, llm_provider: Any | None = None) -> None:
        self.llm_provider = llm_provider
        # Owning pipeline reference (set via :meth:`bind_pipeline`) so the
        # Compact strategy can delegate to the pipeline's _generate_answer.
        self._pipeline: RagPipeline | None = None

    @classmethod
    def select_strategy(cls, n_chunks: int, configured: str = AUTO) -> str:
        """Auto-select a strategy based on the number of chunks.

        ``<= SYNTHESIS_COMPACT_MAX`` -> Compact, ``<= SYNTHESIS_REFINE_MAX`` ->
        Refine, otherwise Tree. An explicit non-auto *configured* value is
        honoured as-is.
        """
        if configured != cls.AUTO:
            return configured
        if n_chunks <= SYNTHESIS_COMPACT_MAX:
            return cls.COMPACT
        if n_chunks <= SYNTHESIS_REFINE_MAX:
            return cls.REFINE
        return cls.TREE

    def synthesize(
        self,
        question: str,
        chunks: list[dict[str, Any]],
        context_window: ContextWindow,
        strategy: str = COMPACT,
    ) -> str:
        """Produce an answer using *strategy*."""
        if not chunks:
            return ""
        if strategy == self.REFINE:
            return self._refine(question, chunks, context_window)
        if strategy == self.TREE:
            return self._tree_summarize(question, chunks, context_window)
        return self._compact(question, chunks, context_window)

    # -- Compact --------------------------------------------------------

    def _compact(
        self,
        question: str,
        chunks: list[dict[str, Any]],
        context_window: ContextWindow,
    ) -> str:
        """Single-pass generation over all concatenated chunks."""
        # Reuse the pipeline's context (which already includes these chunks).
        return self._generate(question, context_window)

    # -- Refine ---------------------------------------------------------

    def _refine(
        self,
        question: str,
        chunks: list[dict[str, Any]],
        context_window: ContextWindow,
    ) -> str:
        """Iteratively refine an intermediate answer with each chunk."""
        if self.llm_provider is None:
            return self._compact(question, chunks, context_window)

        # Seed answer from the first chunk.
        running = self._answer_with_chunk(question, chunks[0], existing_answer="")
        for chunk in chunks[1:]:
            running = self._answer_with_chunk(question, chunk, existing_answer=running)
        return running or self._compact(question, chunks, context_window)

    def _answer_with_chunk(self, question: str, chunk: dict[str, Any], existing_answer: str) -> str:
        """Generate or refine an answer using a single chunk."""
        text = chunk.get("text", "")
        if self.llm_provider is None:
            return existing_answer or text
        if existing_answer:
            prompt = (
                "你有一个已有的答案和一些新的检索信息。请用新信息完善/修正已有答案，"
                "使其更准确、更完整。如果新信息无关，保持原答案。\n\n"
                f"问题：{question}\n\n"
                f"已有答案：{existing_answer}\n\n"
                f"新信息：{text}\n\n"
                "完善后的答案："
            )
        else:
            prompt = (
                f"请基于以下检索信息回答问题。\n\n问题：{question}\n\n检索信息：{text}\n\n答案："
            )
        try:
            messages = [{"role": "user", "content": prompt}]
            answer = self.llm_provider.chat_completion_sync(messages)
            if answer and answer.strip():
                return answer.strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("Refine step failed, keeping running answer: %s", e)
        return existing_answer or text

    # -- Tree Summarize -------------------------------------------------

    def _tree_summarize(
        self,
        question: str,
        chunks: list[dict[str, Any]],
        context_window: ContextWindow,
    ) -> str:
        """Recursively pair-merge chunk answers into a single answer."""
        if self.llm_provider is None:
            return self._compact(question, chunks, context_window)

        # Each leaf is the chunk's own mini-summary relative to the question.
        nodes: list[str] = []
        for chunk in chunks:
            summary = self._answer_with_chunk(question, chunk, existing_answer="")
            nodes.append(summary or chunk.get("text", ""))

        # Pairwise merge until one node remains.
        while len(nodes) > 1:
            next_nodes: list[str] = []
            for i in range(0, len(nodes), 2):
                if i + 1 < len(nodes):
                    merged = self._merge_pair(question, nodes[i], nodes[i + 1])
                    next_nodes.append(merged)
                else:
                    next_nodes.append(nodes[i])
            nodes = next_nodes
        return nodes[0] if nodes else self._compact(question, chunks, context_window)

    def _merge_pair(self, question: str, a: str, b: str) -> str:
        """Merge two intermediate answers into one coherent answer."""
        prompt = (
            "请将以下两段关于同一问题的答案合并为一个连贯、完整、无冗余的答案。\n\n"
            f"问题：{question}\n\n"
            f"答案A：{a}\n\n"
            f"答案B：{b}\n\n"
            "合并后的答案："
        )
        try:
            messages = [{"role": "user", "content": prompt}]
            merged = self.llm_provider.chat_completion_sync(messages)
            if merged and merged.strip():
                return merged.strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("Tree merge step failed, concatenating: %s", e)
        return f"{a}\n{b}"

    # -- Shared generation ---------------------------------------------

    def _generate(self, question: str, context_window: ContextWindow) -> str:
        """Generate via the full context window (Compact path).

        Delegates to a pipeline-style generation. The synthesizer is given a
        reference to the owning pipeline via :meth:`bind_pipeline` so it can
        reuse the pipeline's ``_generate_answer`` fallback logic.
        """
        if self._pipeline is not None:
            return self._pipeline._generate_answer(context_window, question)
        if self.llm_provider is None:
            return context_window.retrieved_context[:500] or "未找到相关信息。"
        try:
            messages = [
                {"role": "system", "content": context_window.system_prompt},
                {
                    "role": "system",
                    "content": f"检索到的相关信息：\n{context_window.retrieved_context}",
                },
                {"role": "user", "content": question},
            ]
            answer = self.llm_provider.chat_completion_sync(messages)
            return answer or context_window.retrieved_context[:500] or "未找到相关信息。"
        except Exception as e:  # noqa: BLE001
            logger.warning("Synthesis generation failed: %s", e)
            return context_window.retrieved_context[:500] or "未找到相关信息。"

    def bind_pipeline(self, pipeline: RagPipeline) -> None:
        """Attach the owning pipeline so synthesis can reuse its generation."""
        self._pipeline = pipeline


# ---------------------------------------------------------------------------
# Sub-Question Engine
# ---------------------------------------------------------------------------


class SubQuestionEngine:
    """Decompose a complex question into sub-questions and synthesise answers.

    Pipeline:
      1. Ask the LLM whether the question warrants decomposition.
      2. If yes, decompose into ``[SUBQUESTION_MIN, SUBQUESTION_MAX]`` sub-Qs.
      3. For each sub-question: retrieve + synthesise a sub-answer.
      4. Merge all sub-answers into one final answer.

    This implements multi-hop reasoning: each hop is an independent retrieval
    over the corpus, and the final merge composes the multi-hop chain. The
    engine reuses the owning pipeline's retriever / synthesiser so all
    advanced features (ANN, reranking, etc.) apply per hop.
    """

    def __init__(self, pipeline: RagPipeline) -> None:
        self.pipeline = pipeline

    def should_decompose(self, query: str) -> bool:
        """Use the LLM to judge whether *query* needs decomposition.

        Returns ``False`` when no LLM is available (so simple questions never
        pay the decomposition cost) or when the LLM declines.
        """
        llm = self.pipeline.llm_provider
        if llm is None:
            return False
        prompt = (
            "判断下面的问题是否需要分解为多个子问题来回答（例如涉及多步推理、"
            "多个实体、对比、或“并总结”类问题需要分解；单一事实查找不需要）。\n"
            "只回答“是”或“否”。\n\n"
            f"问题：{query}\n\n"
            "判断："
        )
        try:
            messages = [{"role": "user", "content": prompt}]
            resp = llm.chat_completion_sync(messages)
            if resp:
                return resp.strip().startswith("是")
        except Exception:  # noqa: BLE001
            return False
        return False

    def decompose(self, query: str) -> list[str]:
        """Decompose *query* into bounded sub-questions via the LLM."""
        subs = self.pipeline.query_transformer.decompose_query(query)
        if not subs or subs == [query]:
            return []
        # Bound to [SUBQUESTION_MIN, SUBQUESTION_MAX].
        subs = subs[:SUBQUESTION_MAX]
        if len(subs) < SUBQUESTION_MIN:
            return []
        return subs

    def answer(self, query: str, top_k: int = FINAL_K, session_id: str | None = None) -> str:
        """Run the full sub-question pipeline and return the merged answer."""
        subs = self.decompose(query)
        if not subs:
            return ""

        sub_answers: list[str] = []
        for sub in subs:
            # Per-hop retrieval (no further recursion to avoid blow-up).
            chunks = self.pipeline.retriever.retrieve(sub, top_k=INITIAL_RETRIEVAL_K)
            seen: set[str] = set()
            unique: list[dict[str, Any]] = []
            for c in chunks:
                cid = c.get("chunk_id", "")
                if cid and cid not in seen:
                    seen.add(cid)
                    unique.append(c)
            if not unique:
                sub_answers.append(f"关于“{sub}”：未找到相关信息。")
                continue
            unique = unique[:top_k]
            ctx = self.pipeline.context_engineer.build_context(
                question=sub,
                retrieved_chunks=unique,
                session_id=session_id,
                include_memory=False,
            )
            strategy = ResponseSynthesizer.select_strategy(
                len(unique), self.pipeline.config.synthesis_strategy
            )
            sub_answer = self.pipeline.synthesizer.synthesize(sub, unique, ctx, strategy=strategy)
            sub_answers.append(f"关于“{sub}”：{sub_answer}")

        return self._merge_final(query, subs, sub_answers)

    def _merge_final(self, query: str, subs: list[str], sub_answers: list[str]) -> str:
        """Merge per-sub-question answers into a final composed answer."""
        llm = self.pipeline.llm_provider
        if llm is None:
            return "\n\n".join(sub_answers)
        parts = "\n\n".join(
            f"子问题：{s}\n答案：{a}" for s, a in zip(subs, sub_answers, strict=True)
        )
        prompt = (
            "下面是对一个复杂问题分解后各子问题的答案。请综合这些子答案，"
            "给出对原始问题的完整、连贯的最终答案，去除冗余并保持逻辑一致。\n\n"
            f"原始问题：{query}\n\n"
            f"{parts}\n\n"
            "最终答案："
        )
        try:
            messages = [{"role": "user", "content": prompt}]
            final = llm.chat_completion_sync(messages)
            if final and final.strip():
                return final.strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("Sub-question final merge failed, concatenating: %s", e)
        return "\n\n".join(sub_answers)


# ---------------------------------------------------------------------------
# Main RAG Pipeline
# ---------------------------------------------------------------------------


class RagPipeline:
    """Complete RAG pipeline with context engineering and memory.

    Advanced upgrades (all opt-in via :class:`RagConfig`):

    * **Multi-Query + RRF** — generate diverse query rewrites, retrieve for
      each, and fuse the rankings with Reciprocal Rank Fusion (replaces the
      simple ``expand_query`` loop when ``enable_multi_query`` is on).
    * **HyDE** — retrieve with hypothetical-document embeddings instead of the
      raw query when ``enable_hyde`` is on.
    * **Step-Back** — additionally retrieve for a more abstract "step-back"
      question when ``enable_step_back`` is on.
    * **Sub-question routing** — when ``enable_subquestion`` is on, the LLM
      decides whether a question warrants decomposition; if so the
      :class:`SubQuestionEngine` runs a multi-hop retrieval-then-merge.
    * **Response synthesis** — Compact / Refine / Tree-Summarize strategies,
      auto-selected by chunk count or pinned by ``config.synthesis_strategy``.
    * **Evaluation** — when ``enable_rag_evaluation`` is on, retrieval and
      generation metrics are computed and returned in
      :attr:`RagResponse.evaluation_metrics`.
    * **Streaming** — :meth:`ask_stream` yields incremental progress and
      answer chunks.
    """

    def __init__(
        self,
        db_path: Path,
        embedding_provider: Any | None = None,
        llm_provider: Any | None = None,
        tenant_id: str = "default",
        config: RagConfig | None = None,
        task_store: Any | None = None,
        audit_logger: Any | None = None,
    ) -> None:
        self.db_path = db_path
        self.embedding_provider = embedding_provider
        self.llm_provider = llm_provider
        self.tenant_id = tenant_id
        self.config = config if config is not None else RagConfig()
        self.task_store = task_store
        self.audit_logger = audit_logger

        # Wire the config + providers into every subsystem so that advanced
        # features are consistently enabled end-to-end.
        self.memory = MemorySystem(db_path, tenant_id, embedding_provider=embedding_provider)
        self.context_engineer = ContextEngineer(
            self.memory,
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            config=self.config,
        )
        self.query_transformer = QueryTransformer(llm_provider)
        self.retriever = HybridRetriever(
            db_path,
            embedding_provider,
            tenant_id,
            config=self.config,
            task_store=task_store,
        )
        self.reranker = Reranker()
        self.chunker = SemanticChunker()

        # Response synthesis + sub-question engines (bound to this pipeline).
        self.synthesizer = ResponseSynthesizer(llm_provider)
        self.synthesizer.bind_pipeline(self)
        self.subquestion_engine = SubQuestionEngine(self)

    def ask(
        self,
        question: str,
        session_id: str | None = None,
        top_k: int = FINAL_K,
        use_reranker: bool = True,
        use_query_expansion: bool = True,
        use_memory: bool = True,
        use_hyde: bool | None = None,
        use_step_back: bool | None = None,
        use_multi_query: bool | None = None,
        use_subquestion: bool | None = None,
    ) -> RagResponse:
        """Answer a question using RAG with context engineering.

        Args:
            question: User's question
            session_id: Conversation session ID (for memory)
            top_k: Number of final results
            use_reranker: Whether to use cross-encoder re-ranking
            use_query_expansion: Whether to expand query for better recall
            use_memory: Whether to use memory system
            use_hyde: Override the config flag for HyDE retrieval. When
                ``None`` (default) the :class:`RagConfig` flag is used.
            use_step_back: Override for step-back prompting.
            use_multi_query: Override for multi-query + RRF fusion. When on,
                this *replaces* the simple ``expand_query`` loop.
            use_subquestion: Override for sub-question decomposition routing.

        Returns:
            RagResponse with answer, citations, and (optionally) evaluation
            metrics.
        """
        # Resolve per-call overrides against the config defaults.
        do_hyde = self.config.enable_hyde if use_hyde is None else use_hyde
        do_step_back = self.config.enable_step_back if use_step_back is None else use_step_back
        do_multi_query = (
            self.config.enable_multi_query if use_multi_query is None else use_multi_query
        )
        do_subquestion = (
            self.config.enable_subquestion if use_subquestion is None else use_subquestion
        )

        # Create session if not provided
        if session_id is None and use_memory:
            session_id = self.memory.create_session()

        # ── Sub-question routing ───────────────────────────────────────
        # When enabled and the LLM judges the question complex, decompose into
        # sub-questions, retrieve + synthesise each, and merge. This is the
        # multi-hop reasoning path.
        if do_subquestion and self.subquestion_engine.should_decompose(question):
            sub_answer = self.subquestion_engine.answer(
                question, top_k=top_k, session_id=session_id
            )
            if sub_answer:
                return self._finalise_response(
                    question=question,
                    answer=sub_answer,
                    chunks=[],
                    context=None,
                    session_id=session_id,
                    use_memory=use_memory,
                    strategy=ResponseSynthesizer.COMPACT,
                )

        # ── Step 1: Query transformation ───────────────────────────────
        # (a) HyDE: generate hypothetical answer document(s).
        hyde_docs: list[str] = []
        if do_hyde:
            hyde_docs = self.query_transformer.hyde_transform(question)

        # (b) Multi-query: generate diverse rewrites for RRF fusion.
        retrieval_queries: list[str]
        if do_multi_query:
            retrieval_queries = self.query_transformer.multi_query(question)
        elif use_query_expansion and not hyde_docs:
            retrieval_queries = self.query_transformer.expand_query(question)
        else:
            retrieval_queries = [question]

        # (c) Step-back: derive a broader abstract question.
        step_back: str | None = None
        if do_step_back:
            step_back = self.query_transformer.step_back_query(question)

        # ── Step 2: Hybrid retrieval ───────────────────────────────────
        all_chunks: list[dict[str, Any]] = []

        if hyde_docs:
            for doc in hyde_docs:
                all_chunks.extend(self.retriever.retrieve(doc, top_k=INITIAL_RETRIEVAL_K))
            for q in retrieval_queries:
                all_chunks.extend(self.retriever.retrieve(q, top_k=INITIAL_RETRIEVAL_K))
        elif do_multi_query and len(retrieval_queries) > 1:
            ranked_lists: list[list[dict[str, Any]]] = [
                self.retriever.retrieve(q, top_k=INITIAL_RETRIEVAL_K) for q in retrieval_queries
            ]
            all_chunks = QueryTransformer.rrf_fuse(ranked_lists)
        else:
            for q in retrieval_queries:
                all_chunks.extend(self.retriever.retrieve(q, top_k=INITIAL_RETRIEVAL_K))

        if step_back:
            all_chunks.extend(self.retriever.retrieve(step_back, top_k=INITIAL_RETRIEVAL_K))

        # Deduplicate by chunk_id (preserve first-seen order).
        seen: set[str] = set()
        unique_chunks: list[dict[str, Any]] = []
        for chunk in all_chunks:
            cid = chunk.get("chunk_id", "")
            if cid and cid not in seen:
                seen.add(cid)
                unique_chunks.append(chunk)

        if not unique_chunks:
            return self._no_results_response(question, session_id, use_memory)

        # ── Step 3: Re-ranking ─────────────────────────────────────────
        if use_reranker and len(unique_chunks) > top_k:
            unique_chunks = self.reranker.rerank(question, unique_chunks, top_k=top_k)
        else:
            unique_chunks = unique_chunks[:top_k]

        # ── Step 4: Build context with context engineering ─────────────
        context = self.context_engineer.build_context(
            question=question,
            retrieved_chunks=unique_chunks,
            session_id=session_id,
            include_memory=use_memory,
        )

        # ── Step 5: Generate answer (with synthesis strategy) ──────────
        strategy = ResponseSynthesizer.select_strategy(
            len(unique_chunks), self.config.synthesis_strategy
        )
        answer = self.synthesizer.synthesize(question, unique_chunks, context, strategy=strategy)
        if not answer:
            answer = self._generate_answer(context, question)

        return self._finalise_response(
            question=question,
            answer=answer,
            chunks=unique_chunks,
            context=context,
            session_id=session_id,
            use_memory=use_memory,
            strategy=strategy,
        )

    # ------------------------------------------------------------------
    # ask() helpers (extracted for clarity and reuse by ask_stream)
    # ------------------------------------------------------------------

    def _no_results_response(
        self, question: str, session_id: str | None, use_memory: bool
    ) -> RagResponse:
        """Build the "no results" response, recording the exchange in memory."""
        no_results_msg = "抱歉，我没有在保险库中找到与您问题相关的信息。"
        if session_id and use_memory:
            self.memory.add_turn(session_id, "user", question)
            self.memory.add_turn(session_id, "assistant", no_results_msg)
            self.memory.store_episode(
                session_id=session_id,
                user_message=question,
                assistant_response=no_results_msg,
                context_summary="",
                key_facts=[],
            )
        return RagResponse(
            answer=no_results_msg + "请尝试换一种方式提问。",
            question=question,
            sources=[],
            model_used=self._model_name(),
            retrieval_method="hybrid" if self.embedding_provider else "bm25",
            total_chunks_searched=0,
            conversation_turns=2 if session_id else 0,
            session_id=session_id,
        )

    def _finalise_response(
        self,
        question: str,
        answer: str,
        chunks: list[dict[str, Any]],
        context: ContextWindow | None,
        session_id: str | None,
        use_memory: bool,
        strategy: str,
    ) -> RagResponse:
        """Store memory, build sources, run evaluation, and return RagResponse."""
        if session_id and use_memory:
            self.memory.add_turn(session_id, "user", question)
            self.memory.add_turn(session_id, "assistant", answer)

            key_facts = self._extract_key_facts(answer)
            if key_facts:
                for fact in key_facts:
                    self.memory.store_fact(fact, importance=0.6)

            self.memory.store_episode(
                session_id=session_id,
                user_message=question,
                assistant_response=answer,
                context_summary=answer[:200],
                key_facts=key_facts,
            )

            if context is not None:
                context.conversation_turns += 2

        sources = []
        for i, chunk in enumerate(chunks, 1):
            sources.append(
                RagResult(
                    chunk=chunk,
                    score=chunk.get("score", 0.0),
                    rank=i,
                    source_label=f"来源 {i}",
                )
            )

        # ── Evaluation integration ─────────────────────────────────────
        evaluation_metrics: dict[str, Any] = {}
        if self.config.enable_rag_evaluation:
            evaluation_metrics = self._run_evaluation(
                question=question, answer=answer, chunks=chunks
            )

        total_tokens = context.total_tokens if context is not None else 0
        memory_used = context.memory_used if context is not None else False
        conv_turns = context.conversation_turns if context is not None else (2 if session_id else 0)

        return RagResponse(
            answer=answer,
            question=question,
            sources=sources,
            model_used=self._model_name(),
            retrieval_method="hybrid" if self.embedding_provider else "bm25",
            total_chunks_searched=len(chunks),
            context_tokens_used=total_tokens,
            memory_used=memory_used,
            conversation_turns=conv_turns,
            session_id=session_id,
            evaluation_metrics=evaluation_metrics,
            synthesis_strategy=strategy,
        )

    def _run_evaluation(
        self, question: str, answer: str, chunks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Run RAG evaluation metrics and log results to the audit log.

        Returns a dict mapping metric names to their scores, plus a composite
        ``rag_score``. Failures are logged but never break the pipeline.
        """
        try:
            from doctoragent.model.evaluation import LLMTestCase, RAGEvaluator

            retrieval_context = [c.get("text", "") for c in chunks if c.get("text")]
            test_case = LLMTestCase(
                input=question,
                actual_output=answer,
                retrieval_context=retrieval_context,
            )
            evaluator = RAGEvaluator()
            results = evaluator.evaluate(test_case)
            rag_score = evaluator.evaluate_rag_score(test_case)

            metrics: dict[str, Any] = {
                name: {
                    "score": round(r.score, 4),
                    "passed": r.passed,
                    "reason": r.reason,
                }
                for name, r in results.items()
            }
            metrics["rag_score"] = round(rag_score, 4)

            if self.audit_logger is not None:
                try:
                    self.audit_logger.log(
                        "storage_backend_operation",
                        {
                            "operation": "rag_evaluation",
                            "question": question[:200],
                            "rag_score": round(rag_score, 4),
                            "metrics": {
                                k: v["score"] if isinstance(v, dict) else v
                                for k, v in metrics.items()
                            },
                        },
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("Audit log write for RAG evaluation failed: %s", e)

            return metrics
        except Exception as e:  # noqa: BLE001
            logger.warning("RAG evaluation failed: %s", e)
            return {}

    def _model_name(self) -> str:
        """Best-effort model name from the LLM provider."""
        if self.llm_provider is None:
            return "none"
        return (
            getattr(self.llm_provider, "model_name", None)
            or getattr(getattr(self.llm_provider, "connection", None), "model_name", None)
            or "unknown"
        )

    # ------------------------------------------------------------------
    # Streaming output
    # ------------------------------------------------------------------

    async def ask_stream(
        self,
        question: str,
        session_id: str | None = None,
        top_k: int = FINAL_K,
        use_reranker: bool = True,
        use_query_expansion: bool = True,
        use_memory: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async generator yielding incremental RAG progress and answer chunks.

        Yields a sequence of dict events:

        * ``{"type": "status", "message": "retrieving..."}`` — retrieval started
        * ``{"type": "retrieved", "chunks": [...], "count": N}`` — retrieval done
        * ``{"type": "status", "message": "context assembled"}`` — context built
        * ``{"type": "status", "message": "generating..."}`` — generation started
        * ``{"type": "token", "content": "..."}`` — one answer chunk (word/char)
        * ``{"type": "done", "response": RagResponse}`` — final response

        When the LLM provider exposes a native ``stream_chat_completion``
        async generator it is used for true token streaming; otherwise the
        full answer is generated synchronously and then chunked into words to
        simulate streaming. The simulation keeps the interface uniform.
        """
        if session_id is None and use_memory:
            session_id = self.memory.create_session()

        # ── Retrieval ──────────────────────────────────────────────────
        yield {"type": "status", "message": "retrieving..."}

        queries = [question]
        if use_query_expansion:
            queries = self.query_transformer.expand_query(question)

        all_chunks: list[dict[str, Any]] = []
        for q in queries:
            all_chunks.extend(self.retriever.retrieve(q, top_k=INITIAL_RETRIEVAL_K))

        seen: set[str] = set()
        unique_chunks: list[dict[str, Any]] = []
        for chunk in all_chunks:
            cid = chunk.get("chunk_id", "")
            if cid and cid not in seen:
                seen.add(cid)
                unique_chunks.append(chunk)

        yield {"type": "retrieved", "chunks": unique_chunks, "count": len(unique_chunks)}

        if not unique_chunks:
            response = self._no_results_response(question, session_id, use_memory)
            for word in response.answer.split():
                yield {"type": "token", "content": word + " "}
            yield {"type": "done", "response": response}
            return

        # ── Re-ranking ─────────────────────────────────────────────────
        if use_reranker and len(unique_chunks) > top_k:
            unique_chunks = self.reranker.rerank(question, unique_chunks, top_k=top_k)
        else:
            unique_chunks = unique_chunks[:top_k]

        # ── Context ────────────────────────────────────────────────────
        context = self.context_engineer.build_context(
            question=question,
            retrieved_chunks=unique_chunks,
            session_id=session_id,
            include_memory=use_memory,
        )
        yield {"type": "status", "message": "context assembled"}

        # ── Generation (streamed or simulated) ────────────────────────
        yield {"type": "status", "message": "generating..."}

        strategy = ResponseSynthesizer.select_strategy(
            len(unique_chunks), self.config.synthesis_strategy
        )

        # Try native streaming when the provider supports it and the
        # strategy is Compact (streaming Refine/Tree is not meaningful).
        answer = ""
        native_streamer = getattr(self.llm_provider, "stream_chat_completion", None)
        if (
            native_streamer is not None
            and strategy == ResponseSynthesizer.COMPACT
            and self.llm_provider is not None
        ):
            try:
                messages = self._build_streaming_messages(context, question)
                async for token in native_streamer(messages):
                    answer += token
                    yield {"type": "token", "content": token}
            except Exception as e:  # noqa: BLE001 — fall back to sync
                logger.warning("Native streaming failed, falling back: %s", e)
                answer = ""
        if not answer:
            # Sync generation then simulated word-by-word streaming.
            answer = self.synthesizer.synthesize(
                question, unique_chunks, context, strategy=strategy
            )
            if not answer:
                answer = self._generate_answer(context, question)
            # Simulate streaming by yielding words with a tiny yield between
            # them so consumers see incremental progress.
            for word in answer.split():
                yield {"type": "token", "content": word + " "}

        response = self._finalise_response(
            question=question,
            answer=answer,
            chunks=unique_chunks,
            context=context,
            session_id=session_id,
            use_memory=use_memory,
            strategy=strategy,
        )
        yield {"type": "done", "response": response}

    def _build_streaming_messages(
        self, context: ContextWindow, question: str
    ) -> list[dict[str, Any]]:
        """Build the message list for a streaming LLM call (Compact strategy)."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": context.system_prompt},
        ]
        if context.conversation_history:
            messages.append(
                {
                    "role": "system",
                    "content": f"对话历史：\n{context.conversation_history}",
                }
            )
        if context.retrieved_context:
            messages.append(
                {
                    "role": "system",
                    "content": f"检索到的相关信息：\n{context.retrieved_context}",
                }
            )
        messages.append({"role": "user", "content": question})
        return messages

    def _generate_answer(self, context: ContextWindow, question: str) -> str:
        """Generate answer using LLM with context engineering."""
        if self.llm_provider is None:
            return self._generate_fallback_answer(question, context)

        try:
            # Build messages with context engineering
            messages = [
                {"role": "system", "content": context.system_prompt},
            ]

            # Add conversation history
            if context.conversation_history:
                messages.append(
                    {"role": "system", "content": f"对话历史：\n{context.conversation_history}"}
                )

            # Add retrieved context
            if context.retrieved_context:
                messages.append(
                    {
                        "role": "system",
                        "content": f"检索到的相关信息：\n{context.retrieved_context}",
                    }
                )

            # Add user query
            messages.append({"role": "user", "content": question})

            # Generate response using sync wrapper
            answer = self.llm_provider.chat_completion_sync(messages)

            return answer or self._generate_fallback_answer(question, context)
        except Exception as e:
            logger.warning("LLM generation failed: %s", e)
            return self._generate_fallback_answer(question, context)

    def _generate_fallback_answer(self, question: str, context: ContextWindow) -> str:
        """Generate fallback answer without LLM."""
        if not context.retrieved_context:
            return "未找到相关信息。"

        return f"找到相关信息：\n\n{context.retrieved_context[:500]}..."

    def _extract_key_facts(self, answer: str) -> list[str]:
        """Extract key facts from answer for memory storage."""
        facts = []

        # Simple extraction: look for sentences with key patterns
        patterns = [
            r"(?:包含|提到|说明|描述了?|记录了?)(.{10,50})",
            r"(?:文件|文档|合同|报告)(?:中|里|显示)(.{10,50})",
            r"(?:日期|时间|金额|数量)(?:是|为|等于)(.{5,30})",
        ]

        for pattern in patterns:
            matches = re.findall(pattern, answer)
            facts.extend(matches[:2])

        return facts[:5]  # Limit to 5 facts


# ---------------------------------------------------------------------------
# Chunk Storage
# ---------------------------------------------------------------------------


class ChunkStorage:
    """Store and retrieve text chunks.

    Supports parent-child chunk association: when a chunk dict carries a
    ``parent_chunk_id``, it is persisted so the retriever can later expand a
    precise small-chunk match to its wider parent context.
    """

    def __init__(self, db_path: Path, tenant_id: str = "default") -> None:
        self.db_path = db_path
        self.tenant_id = tenant_id
        self._schema_checked = False

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection."""
        conn = open_sqlite(self.db_path)
        if not self._schema_checked:
            self._ensure_schema(conn)
            self._schema_checked = True
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Idempotently add the ``parent_chunk_id`` column if missing.

        The column is normally created by :class:`TaskStore`, but
        ``ChunkStorage`` may be used standalone (e.g. in tests) against a DB
        that predates the migration, so we guard here too.
        """
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(vault_chunks)").fetchall()}
            if columns and "parent_chunk_id" not in columns:
                conn.execute("ALTER TABLE vault_chunks ADD COLUMN parent_chunk_id TEXT")
                conn.commit()
        except sqlite3.Error:
            # Table doesn't exist yet — it will be created by TaskStore.
            pass

    def store_chunk(self, chunk: dict[str, Any], embedding: list[float] | None = None) -> None:
        """Store a chunk, including its optional ``parent_chunk_id``."""
        embedding_blob = None
        if embedding:
            embedding_blob = struct.pack(f"{len(embedding)}d", *embedding)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO vault_chunks
                    (chunk_id, task_id, vault_path, category, summary, chunk_index,
                     text, start_char, end_char, embedding, model, created_at,
                     tenant_id, parent_chunk_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
                """,
                (
                    chunk.get("chunk_id", ""),
                    chunk.get("task_id", ""),
                    chunk.get("vault_path", ""),
                    chunk.get("category", ""),
                    chunk.get("summary", ""),
                    chunk.get("chunk_index", 0),
                    chunk.get("text", ""),
                    chunk.get("start_char", 0),
                    chunk.get("end_char", 0),
                    embedding_blob,
                    chunk.get("model", "unknown"),
                    self.tenant_id,
                    chunk.get("parent_chunk_id"),
                ),
            )
            conn.commit()

    def get_chunks_by_task(self, task_id: str) -> list[dict[str, Any]]:
        """Get chunks for a task, including ``parent_chunk_id``."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chunk_id, task_id, vault_path, category, summary, "
                "chunk_index, text, start_char, end_char, parent_chunk_id "
                "FROM vault_chunks WHERE task_id = ? AND tenant_id = ?",
                (task_id, self.tenant_id),
            ).fetchall()

        return [
            {
                "chunk_id": row[0],
                "task_id": row[1],
                "vault_path": row[2],
                "category": row[3],
                "summary": row[4],
                "chunk_index": row[5],
                "text": row[6],
                "start_char": row[7],
                "end_char": row[8],
                "parent_chunk_id": row[9],
            }
            for row in rows
        ]
