"""Adaptive Query Router for DoctorAgent RAG.

Classifies an incoming query and selects the most effective retrieval
strategy (keyword, semantic, hybrid, multi-hop, graph-based, or agentic)
along with retrieval tuning (``top_k``, reranking, decomposition, ...).

The router uses a fast rule-based classifier first (keyword matching) and
only falls back to an LLM when the rules are inconclusive. The LLM path
asks for structured JSON output parsed with the ``_extract_json`` helper
from :mod:`doctoragent.model.agent`.

Example::

    router = QueryRouter(llm_provider)
    qtype = router.classify_query("compare Postgres vs MySQL")
    config = router.get_retrieval_config(qtype)
    # -> RetrievalStrategy.MULTI_HOP, use_graph=True, decompose=True
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from doctoragent.compat import StrEnum
from doctoragent.model.agent import _extract_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class QueryType(StrEnum):
    """Coarse classification of a user query's intent."""

    FACTUAL = "factual"
    ANALYTICAL = "analytical"
    COMPARATIVE = "comparative"
    TEMPORAL = "temporal"
    RELATIONAL = "relational"
    PROCEDURAL = "procedural"


class RetrievalStrategy(StrEnum):
    """Retrieval approach selected for a query type."""

    KEYWORD_ONLY = "keyword_only"
    SEMANTIC_ONLY = "semantic_only"
    HYBRID = "hybrid"
    MULTI_HOP = "multi_hop"
    GRAPH_BASED = "graph_based"
    AGENTIC = "agentic"


# ---------------------------------------------------------------------------
# Rule-based keyword tables
# ---------------------------------------------------------------------------

_COMPARISON_KEYWORDS: tuple[str, ...] = (
    " vs ",
    "vs.",
    "versus",
    "compare",
    "comparison",
    "difference",
    "differ",
    "contrast",
    "better than",
    "worse than",
    "pros and cons",
    "advantages",
    "disadvantages",
)

_TEMPORAL_KEYWORDS: tuple[str, ...] = (
    "when",
    "what date",
    "what year",
    "how long",
    "how old",
    "latest",
    "newest",
    "oldest",
    "first",
    "last ",
    "timeline",
    "chronolog",
    "before",
    "after",
    "since",
    "until",
    "recently",
    "yesterday",
    "today",
    "tomorrow",
)

_RELATIONAL_KEYWORDS: tuple[str, ...] = (
    "related to",
    "relation",
    "relationship",
    "connection",
    "connected",
    "between",
    " linked",
    "link to",
    "associate",
    "associated",
    "depend",
    "impact of",
    "effect of",
    "involve",
)

_PROCEDURAL_KEYWORDS: tuple[str, ...] = (
    "how to",
    "how do",
    "how can",
    "how could",
    "steps",
    "step by",
    "procedure",
    "process",
    "instructions",
    "guide",
    "tutorial",
    "setup",
    "set up",
    "configure",
    "install",
    "deploy",
    "run",
)

_ANALYTICAL_KEYWORDS: tuple[str, ...] = (
    "why",
    "analyze",
    "analyse",
    "analysis",
    "impact",
    "implication",
    "consequence",
    "reason",
    "cause",
    "effect of",
    "evaluate",
    "assess",
    "trade-off",
    "tradeoff",
    "strategy",
)

_FACTUAL_KEYWORDS: tuple[str, ...] = (
    "what is",
    "what are",
    "who is",
    "who are",
    "where is",
    "where are",
    "define",
    "definition",
    "meaning of",
    "stands for",
    "refer to",
)


# ---------------------------------------------------------------------------
# Retrieval configuration
# ---------------------------------------------------------------------------


@dataclass
class RetrievalConfig:
    """Tuning parameters derived from a :class:`QueryType`."""

    strategy: RetrievalStrategy = RetrievalStrategy.HYBRID
    top_k: int = 5
    rerank: bool = True
    use_graph: bool = False
    decompose: bool = False
    use_memory: bool = True
    extra: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for logging / debugging."""
        return {
            "strategy": str(self.strategy),
            "top_k": self.top_k,
            "rerank": self.rerank,
            "use_graph": self.use_graph,
            "decompose": self.decompose,
            "use_memory": self.use_memory,
            "extra": dict(self.extra),
        }


# Per-query-type retrieval configuration.
_DEFAULT_CONFIGS: dict[QueryType, RetrievalConfig] = {
    QueryType.FACTUAL: RetrievalConfig(
        strategy=RetrievalStrategy.HYBRID,
        top_k=5,
        rerank=True,
    ),
    QueryType.ANALYTICAL: RetrievalConfig(
        strategy=RetrievalStrategy.MULTI_HOP,
        top_k=8,
        rerank=True,
        decompose=True,
    ),
    QueryType.COMPARATIVE: RetrievalConfig(
        strategy=RetrievalStrategy.MULTI_HOP,
        top_k=8,
        rerank=True,
        use_graph=True,
        decompose=True,
    ),
    QueryType.TEMPORAL: RetrievalConfig(
        strategy=RetrievalStrategy.KEYWORD_ONLY,
        top_k=5,
        rerank=False,
    ),
    QueryType.RELATIONAL: RetrievalConfig(
        strategy=RetrievalStrategy.GRAPH_BASED,
        top_k=6,
        rerank=True,
        use_graph=True,
    ),
    QueryType.PROCEDURAL: RetrievalConfig(
        strategy=RetrievalStrategy.HYBRID,
        top_k=6,
        rerank=True,
        decompose=True,
    ),
}

# Query type -> retrieval strategy (used by :meth:`QueryRouter.route`).
_TYPE_TO_STRATEGY: dict[QueryType, RetrievalStrategy] = {
    qtype: cfg.strategy for qtype, cfg in _DEFAULT_CONFIGS.items()
}


def _llm_response_text(response: Any) -> str:
    """Normalize an LLM provider response to plain text."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    return getattr(response, "content", "") or ""


# ---------------------------------------------------------------------------
# Query Router
# ---------------------------------------------------------------------------


class QueryRouter:
    """Classify queries and route them to the best retrieval strategy.

    The classifier is layered:

    1. **Rules** — fast keyword matching handles the common cases without
       any LLM round-trip.
    2. **LLM** — when the rules are inconclusive and a provider is given,
       the LLM classifies the query via structured JSON output.

    Callers may pass an ``llm_provider`` either at construction time or per
    call (the per-call value wins).
    """

    def __init__(self, llm_provider: Any | None = None) -> None:
        self.llm_provider = llm_provider

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def classify_query(
        self,
        query: str,
        llm_provider: Any | None = None,
    ) -> QueryType:
        """Classify *query* into a :class:`QueryType`.

        Args:
            query: The user query text.
            llm_provider: Optional override provider; falls back to the one
                supplied at construction. When ``None`` and no construction
                provider exists, only the rule-based path is used.
        """
        if not query or not query.strip():
            return QueryType.FACTUAL

        rule_based = self._classify_with_rules(query)
        if rule_based is not None:
            return rule_based

        provider = llm_provider or self.llm_provider
        if provider is not None:
            llm_based = self._classify_with_llm(query, provider)
            if llm_based is not None:
                return llm_based

        return QueryType.FACTUAL

    def _classify_with_rules(self, query: str) -> QueryType | None:
        """Rule-based classification using keyword tables.

        Returns ``None`` when no rule matches (caller then tries the LLM).
        Comparison is checked first because comparative queries often also
        contain relational language ("connection between A and B").
        """
        qlower = f" {query.lower()} "
        # Normalize a couple of common token boundaries for matching.
        normalized = " " + " ".join(query.lower().split()) + " "

        if any(kw in qlower or kw in normalized for kw in _COMPARISON_KEYWORDS):
            return QueryType.COMPARATIVE
        if any(kw in qlower or kw in normalized for kw in _TEMPORAL_KEYWORDS):
            return QueryType.TEMPORAL
        if any(kw in qlower or kw in normalized for kw in _RELATIONAL_KEYWORDS):
            return QueryType.RELATIONAL
        if any(kw in qlower or kw in normalized for kw in _PROCEDURAL_KEYWORDS):
            return QueryType.PROCEDURAL
        if any(kw in qlower or kw in normalized for kw in _ANALYTICAL_KEYWORDS):
            return QueryType.ANALYTICAL
        if any(kw in qlower or kw in normalized for kw in _FACTUAL_KEYWORDS):
            return QueryType.FACTUAL
        return None

    def _classify_with_llm(
        self,
        query: str,
        llm_provider: Any,
    ) -> QueryType | None:
        """LLM-based classification via structured JSON output.

        Returns ``None`` if the LLM is unavailable or returns an unparseable
        / out-of-vocabulary category.
        """
        prompt = (
            "Classify the user query below into exactly one of these "
            "categories:\n"
            "- factual: asks for a definition, fact, or specific detail\n"
            "- analytical: requires reasoning, cause/effect, evaluation\n"
            "- comparative: compares two or more things\n"
            "- temporal: about dates, time, recency, chronology\n"
            "- relational: about connections/relationships between things\n"
            "- procedural: asks how to do something, steps, instructions\n\n"
            "Return ONLY a JSON object: "
            '{"category": "<one of the categories>", "reason": "<short>"}\n\n'
            f"Query: {query}\n"
        )
        try:
            messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            response = llm_provider.chat_completion_sync(messages)
            data = _extract_json(_llm_response_text(response))
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM query classification failed: %s", exc)
            return None

        if not isinstance(data, dict):
            return None
        category = str(data.get("category", "")).strip().lower()
        if not category:
            return None
        try:
            return QueryType(category)
        except ValueError:
            logger.debug("LLM returned unknown query category: %r", category)
            return None

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route(self, query_type: QueryType) -> RetrievalStrategy:
        """Map a :class:`QueryType` to its :class:`RetrievalStrategy`."""
        return _TYPE_TO_STRATEGY.get(query_type, RetrievalStrategy.HYBRID)

    def should_decompose(self, query_type: QueryType) -> bool:
        """Whether the query should be decomposed into sub-questions.

        Complex queries (analytical, comparative, procedural) benefit from
        decomposition so each facet can be retrieved independently and
        fused afterwards.
        """
        return query_type in (QueryType.ANALYTICAL, QueryType.COMPARATIVE, QueryType.PROCEDURAL)

    def should_use_graph(self, query_type: QueryType) -> bool:
        """Whether retrieval should consult the knowledge graph.

        Relational and comparative queries are exactly the cases where
        structured entity links add signal beyond lexical / dense matching.
        """
        return query_type in (QueryType.RELATIONAL, QueryType.COMPARATIVE)

    def get_retrieval_config(self, query_type: QueryType) -> RetrievalConfig:
        """Return the full retrieval tuning for *query_type*.

        The returned :class:`RetrievalConfig` is a *copy* so callers may
        safely mutate it without affecting the shared defaults.
        """
        base = _DEFAULT_CONFIGS.get(query_type, _DEFAULT_CONFIGS[QueryType.FACTUAL])
        return RetrievalConfig(
            strategy=base.strategy,
            top_k=base.top_k,
            rerank=base.rerank,
            use_graph=base.use_graph,
            decompose=base.decompose,
            use_memory=base.use_memory,
            extra=dict(base.extra),
        )
