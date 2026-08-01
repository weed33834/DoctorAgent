# mypy: ignore-errors
"""Tests for the adaptive query router, corrective RAG, agentic RAG, and caching.

Covers:
* :class:`QueryRouter` — rule-based and LLM-based query classification,
  routing, decomposition/graph flags and retrieval config.
* :class:`CorrectiveRAG` — retrieval evaluation, document grading, query
  rewriting and the full correction loop.
* :class:`AgenticRAG` — action decision, action execution and the main
  controller loop.
* :class:`TTLCache`, :class:`EmbeddingCache`, :class:`QueryResultCache` —
  TTL expiry, LRU eviction, stats and document-level invalidation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from doctoragent.model.query_router import (
    QueryRouter,
    QueryType,
    RetrievalConfig,
    RetrievalStrategy,
)
from doctoragent.model.corrective_rag import (
    CorrectiveRAG,
    DocumentGrade,
    RetrievalAssessment,
    RetrievalEvaluation,
)
from doctoragent.model.agentic_rag import (
    ActionDecision,
    AgenticRAG,
    AgenticRAGState,
    RAGAction,
)
from doctoragent.model.cache import (
    EmbeddingCache,
    QueryResultCache,
    TTLCache,
)


# ---------------------------------------------------------------------------
# Mock LLM provider
# ---------------------------------------------------------------------------

class MockLLMProvider:
    """Minimal LLM provider returning canned responses for deterministic tests."""

    def __init__(self, responses: list[str] | None = None, default: str = "") -> None:
        self._responses = responses or []
        self._idx = 0
        self.default = default
        self.model_name = "mock-model"
        self.call_count = 0

    def chat_completion_sync(self, messages: list[dict[str, Any]]) -> str:
        self.call_count += 1
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return self.default

    async def chat_completion(self, messages: list[dict[str, Any]]) -> str:
        return self.chat_completion_sync(messages)


class AsyncMockLLMProvider:
    """LLM provider whose ``chat_completion`` is a native coroutine."""

    def __init__(self, responses: list[str] | None = None, default: str = "") -> None:
        self._responses = responses or []
        self._idx = 0
        self.default = default
        self.model_name = "mock-async-model"
        self.call_count = 0

    async def chat_completion(self, messages: list[dict[str, Any]]) -> str:
        self.call_count += 1
        await asyncio.sleep(0)
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return self.default

    def chat_completion_sync(self, messages: list[dict[str, Any]]) -> str:
        self.call_count += 1
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return self.default


# ===========================================================================
# Query Router
# ===========================================================================

class TestQueryRouter:
    """Tests for :class:`QueryRouter`."""

    @pytest.fixture
    def router(self) -> QueryRouter:
        return QueryRouter()

    # -- rule-based classification -----------------------------------------

    def test_classify_factual(self, router: QueryRouter) -> None:
        assert router.classify_query("What is encryption?") == QueryType.FACTUAL
        assert router.classify_query("Who is the CEO?") == QueryType.FACTUAL

    def test_classify_comparative(self, router: QueryRouter) -> None:
        assert router.classify_query("Compare Postgres vs MySQL") == QueryType.COMPARATIVE
        assert router.classify_query("AES vs RSA encryption") == QueryType.COMPARATIVE
        assert router.classify_query("pros and cons of cloud storage") == QueryType.COMPARATIVE

    def test_classify_temporal(self, router: QueryRouter) -> None:
        assert router.classify_query("When was the contract signed?") == QueryType.TEMPORAL
        assert router.classify_query("What date did the project start?") == QueryType.TEMPORAL
        assert router.classify_query("latest updates") == QueryType.TEMPORAL

    def test_classify_relational(self, router: QueryRouter) -> None:
        assert router.classify_query("How is Alice related to Bob?") == QueryType.RELATIONAL
        assert router.classify_query("What is the relationship between these entities?") == QueryType.RELATIONAL

    def test_classify_procedural(self, router: QueryRouter) -> None:
        assert router.classify_query("How to encrypt a file") == QueryType.PROCEDURAL
        assert router.classify_query("Steps to set up the vault") == QueryType.PROCEDURAL
        assert router.classify_query("How do I configure the API?") == QueryType.PROCEDURAL

    def test_classify_empty_query_defaults_to_factual(self, router: QueryRouter) -> None:
        assert router.classify_query("") == QueryType.FACTUAL
        assert router.classify_query("   ") == QueryType.FACTUAL

    def test_classify_unmatched_defaults_to_factual(self, router: QueryRouter) -> None:
        assert router.classify_query("random gibberish xyz") == QueryType.FACTUAL

    def test_classify_without_llm(self, router: QueryRouter) -> None:
        """Rule-based classification should work without any LLM provider."""
        assert router.classify_query("compare A vs B") == QueryType.COMPARATIVE

    # -- LLM-based classification ------------------------------------------

    def test_classify_with_llm(self) -> None:
        llm = MockLLMProvider(responses=[
            json.dumps({"category": "analytical", "reason": "requires reasoning"}),
        ])
        router_with_llm = QueryRouter(llm_provider=llm)
        qtype = router_with_llm.classify_query("unusual query with no keyword match")
        assert qtype == QueryType.ANALYTICAL

    def test_classify_llm_per_call_override(self, router: QueryRouter) -> None:
        llm = MockLLMProvider(responses=[
            json.dumps({"category": "relational", "reason": "connections"}),
        ])
        qtype = router.classify_query("random text with no keywords", llm_provider=llm)
        assert qtype == QueryType.RELATIONAL

    def test_classify_llm_invalid_category_falls_back(self) -> None:
        llm = MockLLMProvider(responses=[
            json.dumps({"category": "unknown_type", "reason": "bad"}),
        ])
        router_with_llm = QueryRouter(llm_provider=llm)
        qtype = router_with_llm.classify_query("random text with no keywords")
        assert qtype == QueryType.FACTUAL

    # -- routing -----------------------------------------------------------

    @pytest.mark.parametrize(
        "query_type,expected_strategy",
        [
            (QueryType.FACTUAL, RetrievalStrategy.HYBRID),
            (QueryType.ANALYTICAL, RetrievalStrategy.MULTI_HOP),
            (QueryType.COMPARATIVE, RetrievalStrategy.MULTI_HOP),
            (QueryType.TEMPORAL, RetrievalStrategy.KEYWORD_ONLY),
            (QueryType.RELATIONAL, RetrievalStrategy.GRAPH_BASED),
            (QueryType.PROCEDURAL, RetrievalStrategy.HYBRID),
        ],
    )
    def test_route(
        self, router: QueryRouter, query_type: QueryType, expected_strategy: RetrievalStrategy
    ) -> None:
        assert router.route(query_type) == expected_strategy

    # -- decomposition / graph flags ---------------------------------------

    def test_should_decompose(self, router: QueryRouter) -> None:
        assert router.should_decompose(QueryType.ANALYTICAL) is True
        assert router.should_decompose(QueryType.COMPARATIVE) is True
        assert router.should_decompose(QueryType.PROCEDURAL) is True
        assert router.should_decompose(QueryType.FACTUAL) is False
        assert router.should_decompose(QueryType.TEMPORAL) is False
        assert router.should_decompose(QueryType.RELATIONAL) is False

    def test_should_use_graph(self, router: QueryRouter) -> None:
        assert router.should_use_graph(QueryType.RELATIONAL) is True
        assert router.should_use_graph(QueryType.COMPARATIVE) is True
        assert router.should_use_graph(QueryType.FACTUAL) is False
        assert router.should_use_graph(QueryType.TEMPORAL) is False

    # -- retrieval config --------------------------------------------------

    def test_get_retrieval_config_factual(self, router: QueryRouter) -> None:
        config = router.get_retrieval_config(QueryType.FACTUAL)
        assert config.strategy == RetrievalStrategy.HYBRID
        assert config.top_k == 5
        assert config.rerank is True

    def test_get_retrieval_config_comparative(self, router: QueryRouter) -> None:
        config = router.get_retrieval_config(QueryType.COMPARATIVE)
        assert config.strategy == RetrievalStrategy.MULTI_HOP
        assert config.top_k == 8
        assert config.use_graph is True
        assert config.decompose is True

    def test_get_retrieval_config_relational(self, router: QueryRouter) -> None:
        config = router.get_retrieval_config(QueryType.RELATIONAL)
        assert config.strategy == RetrievalStrategy.GRAPH_BASED
        assert config.use_graph is True

    def test_get_retrieval_config_returns_copy(self, router: QueryRouter) -> None:
        config1 = router.get_retrieval_config(QueryType.FACTUAL)
        config1.top_k = 999
        config2 = router.get_retrieval_config(QueryType.FACTUAL)
        assert config2.top_k == 5

    def test_retrieval_config_to_dict(self, router: QueryRouter) -> None:
        config = router.get_retrieval_config(QueryType.COMPARATIVE)
        d = config.to_dict()
        assert "strategy" in d
        assert "top_k" in d
        assert "use_graph" in d
        assert d["use_graph"] is True


# ===========================================================================
# Corrective RAG
# ===========================================================================

class TestCorrectiveRAG:
    """Tests for :class:`CorrectiveRAG`."""

    @pytest.fixture
    def crag(self) -> CorrectiveRAG:
        return CorrectiveRAG()

    @pytest.fixture
    def sample_docs(self) -> list[dict[str, str]]:
        return [
            {"doc_id": "d1", "text": "AES is a symmetric encryption algorithm."},
            {"doc_id": "d2", "text": "RSA is an asymmetric encryption algorithm."},
        ]

    # -- evaluate_retrieval ------------------------------------------------

    def test_evaluate_retrieval_correct(self, crag: CorrectiveRAG, sample_docs: list) -> None:
        llm = MockLLMProvider(responses=[
            json.dumps({"score": 0.9, "assessment": "Correct", "reason": "Relevant"}),
        ])
        eval_result = crag.evaluate_retrieval("What is AES?", sample_docs, llm_provider=llm)
        assert eval_result.assessment == RetrievalAssessment.CORRECT
        assert eval_result.score == pytest.approx(0.9)

    def test_evaluate_retrieval_incorrect(self, crag: CorrectiveRAG, sample_docs: list) -> None:
        llm = MockLLMProvider(responses=[
            json.dumps({"score": 0.1, "assessment": "Incorrect", "reason": "Not relevant"}),
        ])
        eval_result = crag.evaluate_retrieval("What is the weather?", sample_docs, llm_provider=llm)
        assert eval_result.assessment == RetrievalAssessment.INCORRECT

    def test_evaluate_retrieval_ambiguous(self, crag: CorrectiveRAG, sample_docs: list) -> None:
        llm = MockLLMProvider(responses=[
            json.dumps({"score": 0.5, "assessment": "Ambiguous", "reason": "Partial"}),
        ])
        eval_result = crag.evaluate_retrieval("Tell me about encryption", sample_docs, llm_provider=llm)
        assert eval_result.assessment == RetrievalAssessment.AMBIGUOUS

    def test_evaluate_retrieval_empty_docs(self, crag: CorrectiveRAG) -> None:
        eval_result = crag.evaluate_retrieval("anything", [])
        assert eval_result.assessment == RetrievalAssessment.INCORRECT
        assert eval_result.score == 0.0

    def test_evaluate_retrieval_no_llm_heuristic(self, crag: CorrectiveRAG, sample_docs: list) -> None:
        """Without an LLM, the heuristic is used (never returns Correct)."""
        eval_result = crag.evaluate_retrieval("encryption algorithm", sample_docs)
        assert eval_result.assessment in (RetrievalAssessment.AMBIGUOUS, RetrievalAssessment.INCORRECT)

    def test_evaluate_retrieval_uses_construction_provider(self, sample_docs: list) -> None:
        llm = MockLLMProvider(responses=[
            json.dumps({"score": 0.85, "assessment": "Correct", "reason": "OK"}),
        ])
        crag_with_llm = CorrectiveRAG(llm_provider=llm)
        eval_result = crag_with_llm.evaluate_retrieval("encryption", sample_docs)
        assert eval_result.assessment == RetrievalAssessment.CORRECT

    # -- grade_document ----------------------------------------------------

    def test_grade_document_relevant(self, crag: CorrectiveRAG) -> None:
        llm = MockLLMProvider(responses=[
            json.dumps({"relevant": True, "score": 0.9, "reason": "Directly addresses query"}),
        ])
        doc = {"text": "AES is a symmetric encryption standard."}
        grade = crag.grade_document("What is AES?", doc, llm_provider=llm)
        assert grade.relevant is True
        assert grade.score == pytest.approx(0.9)

    def test_grade_document_irrelevant(self, crag: CorrectiveRAG) -> None:
        llm = MockLLMProvider(responses=[
            json.dumps({"relevant": False, "score": 0.1, "reason": "Unrelated"}),
        ])
        doc = {"text": "The weather is sunny today."}
        grade = crag.grade_document("What is AES?", doc, llm_provider=llm)
        assert grade.relevant is False

    def test_grade_document_empty(self, crag: CorrectiveRAG) -> None:
        grade = crag.grade_document("query", {"text": ""})
        assert grade.relevant is False
        assert grade.score == 0.0

    def test_grade_document_no_llm_heuristic(self, crag: CorrectiveRAG) -> None:
        doc = {"text": "AES encryption is a symmetric algorithm"}
        grade = crag.grade_document("AES encryption", doc)
        assert grade.relevant is True
        assert grade.score > 0.0

    # -- rewrite_query -----------------------------------------------------

    def test_rewrite_query(self, crag: CorrectiveRAG) -> None:
        llm = MockLLMProvider(responses=["symmetric encryption AES algorithm explained"])
        rewritten = crag.rewrite_query("tell me about AES", llm_provider=llm)
        assert "symmetric" in rewritten
        assert rewritten != "tell me about AES"

    def test_rewrite_query_no_llm(self, crag: CorrectiveRAG) -> None:
        original = "original query"
        rewritten = crag.rewrite_query(original)
        assert rewritten == original

    # -- run_correction_loop -----------------------------------------------

    def test_run_correction_loop_correct_first_try(
        self, crag: CorrectiveRAG
    ) -> None:
        llm = MockLLMProvider(responses=[
            json.dumps({"score": 0.95, "assessment": "Correct", "reason": "Good"}),
        ])
        docs = [{"text": "AES is symmetric encryption"}]

        def retrieve_fn(query: str) -> list:
            return docs

        result = crag.run_correction_loop(
            "What is AES?", retrieve_fn, llm_provider=llm, max_iterations=2
        )
        assert result["evaluation"]["assessment"] == "Correct"
        assert result["iterations"] == 0
        assert result["corrected"] is False
        assert len(result["docs"]) == 1

    def test_run_correction_loop_rewrites_on_incorrect(
        self, crag: CorrectiveRAG
    ) -> None:
        """When retrieval is Incorrect, the query is rewritten and re-retrieved."""
        # First eval = Incorrect, rewrite response, second eval = Correct
        llm = MockLLMProvider(responses=[
            json.dumps({"score": 0.1, "assessment": "Incorrect", "reason": "Bad"}),
            "improved query about AES encryption",  # rewrite
            json.dumps({"score": 0.9, "assessment": "Correct", "reason": "Good"}),
        ])
        call_count = {"n": 0}

        def retrieve_fn(query: str) -> list:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [{"text": "unrelated content"}]
            return [{"text": "AES is a symmetric cipher"}]

        result = crag.run_correction_loop(
            "AES", retrieve_fn, llm_provider=llm, max_iterations=2
        )
        assert result["corrected"] is True
        assert result["iterations"] >= 1
        assert len(result["trace"]) >= 2

    def test_run_correction_loop_no_rewrite_change_stops(
        self, crag: CorrectiveRAG
    ) -> None:
        """When the rewrite is identical, the loop stops."""
        llm = MockLLMProvider(responses=[
            json.dumps({"score": 0.1, "assessment": "Incorrect", "reason": "Bad"}),
            "same query",  # rewrite produces no change
        ])

        def retrieve_fn(query: str) -> list:
            return [{"text": "some content"}]

        result = crag.run_correction_loop(
            "same query", retrieve_fn, llm_provider=llm, max_iterations=3
        )
        # Should stop after one failed rewrite.
        assert result["iterations"] == 0

    def test_should_web_search(self, crag: CorrectiveRAG) -> None:
        incorrect = RetrievalEvaluation(assessment=RetrievalAssessment.INCORRECT)
        correct = RetrievalEvaluation(assessment=RetrievalAssessment.CORRECT)
        assert crag.should_web_search(incorrect) is True
        assert crag.should_web_search(correct) is False

    def test_should_mix_sources(self, crag: CorrectiveRAG) -> None:
        ambiguous = RetrievalEvaluation(assessment=RetrievalAssessment.AMBIGUOUS)
        correct = RetrievalEvaluation(assessment=RetrievalAssessment.CORRECT)
        assert crag.should_mix_sources(ambiguous) is True
        assert crag.should_mix_sources(correct) is False

    def test_retrieval_evaluation_to_dict(self) -> None:
        ev = RetrievalEvaluation(score=0.75, assessment=RetrievalAssessment.CORRECT, reason="ok")
        d = ev.to_dict()
        assert d["score"] == 0.75
        assert d["assessment"] == "Correct"

    def test_document_grade_to_dict(self) -> None:
        g = DocumentGrade(relevant=True, score=0.8, reason="match")
        d = g.to_dict()
        assert d["relevant"] is True
        assert d["score"] == 0.8


# ===========================================================================
# Agentic RAG
# ===========================================================================

class TestAgenticRAG:
    """Tests for :class:`AgenticRAG`."""

    @pytest.fixture
    def mock_retrieve_fn(self):
        async def _retrieve(query: str, top_k: int = 5) -> list[dict[str, Any]]:
            return [
                {"doc_id": "d1", "text": f"Document about {query}"},
                {"doc_id": "d2", "text": "Additional context"},
            ]
        return _retrieve

    @pytest.fixture
    def state(self) -> AgenticRAGState:
        return AgenticRAGState(query="What is encryption?")

    # -- decide_action -----------------------------------------------------

    async def test_decide_action_retrieve_first(
        self, mock_retrieve_fn, state: AgenticRAGState
    ) -> None:
        """With no docs, the fallback picks RETRIEVE."""
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=None, max_iterations=5)
        decision = await agent.decide_action(state)
        assert decision.action == RAGAction.RETRIEVE

    async def test_decide_action_generate_after_retrieve(
        self, mock_retrieve_fn
    ) -> None:
        """With docs but no answer, the fallback picks GENERATE."""
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=None, max_iterations=5)
        state = AgenticRAGState(
            query="encryption",
            retrieved_docs=[{"doc_id": "d1", "text": "AES is symmetric"}],
        )
        decision = await agent.decide_action(state)
        assert decision.action == RAGAction.GENERATE

    async def test_decide_action_terminate_after_answer(
        self, mock_retrieve_fn
    ) -> None:
        """With an answer, the fallback picks TERMINATE."""
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=None, max_iterations=5)
        state = AgenticRAGState(
            query="encryption",
            retrieved_docs=[{"doc_id": "d1", "text": "AES"}],
            current_answer="AES is symmetric encryption.",
        )
        decision = await agent.decide_action(state)
        assert decision.action == RAGAction.TERMINATE

    async def test_decide_action_with_llm(
        self, mock_retrieve_fn, state: AgenticRAGState
    ) -> None:
        llm = AsyncMockLLMProvider(responses=[
            json.dumps({"action": "retrieve", "reason": "need docs", "params": {"top_k": 3}}),
        ])
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=llm, max_iterations=5)
        decision = await agent.decide_action(state)
        assert decision.action == RAGAction.RETRIEVE
        assert decision.params.get("top_k") == 3

    async def test_decide_action_llm_invalid_falls_back(
        self, mock_retrieve_fn, state: AgenticRAGState
    ) -> None:
        llm = AsyncMockLLMProvider(responses=["not json at all"])
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=llm, max_iterations=5)
        decision = await agent.decide_action(state)
        assert decision.action == RAGAction.RETRIEVE

    # -- execute_action ----------------------------------------------------

    async def test_execute_retrieve(
        self, mock_retrieve_fn, state: AgenticRAGState
    ) -> None:
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=None, max_iterations=5)
        decision = ActionDecision(action=RAGAction.RETRIEVE, params={"top_k": 2})
        await agent.execute_action(state, decision)
        assert len(state.retrieved_docs) == 2

    async def test_execute_generate_without_llm(
        self, mock_retrieve_fn
    ) -> None:
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=None, max_iterations=5)
        state = AgenticRAGState(
            query="encryption",
            retrieved_docs=[{"doc_id": "d1", "text": "AES is symmetric"}],
        )
        decision = ActionDecision(action=RAGAction.GENERATE)
        await agent.execute_action(state, decision)
        assert state.current_answer != ""
        assert "AES" in state.current_answer

    async def test_execute_generate_with_llm(
        self, mock_retrieve_fn
    ) -> None:
        llm = AsyncMockLLMProvider(responses=["AES is a symmetric encryption standard."])
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=llm, max_iterations=5)
        state = AgenticRAGState(
            query="What is AES?",
            retrieved_docs=[{"doc_id": "d1", "text": "AES encryption info"}],
        )
        decision = ActionDecision(action=RAGAction.GENERATE)
        await agent.execute_action(state, decision)
        assert "AES" in state.current_answer

    async def test_execute_terminate(self, mock_retrieve_fn, state: AgenticRAGState) -> None:
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=None, max_iterations=5)
        decision = ActionDecision(action=RAGAction.TERMINATE)
        await agent.execute_action(state, decision)
        # TERMINATE does nothing to the state.
        assert state.iteration == 0

    async def test_execute_rewrite_query(
        self, mock_retrieve_fn
    ) -> None:
        llm = AsyncMockLLMProvider(responses=["improved query about AES encryption"])
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=llm, max_iterations=5)
        state = AgenticRAGState(query="AES")
        decision = ActionDecision(action=RAGAction.REWRITE_QUERY, params={"query": "AES encryption explained"})
        await agent.execute_action(state, decision)
        assert state.query == "AES encryption explained"

    # -- run (full loop) ---------------------------------------------------

    async def test_run_without_llm(self, mock_retrieve_fn) -> None:
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=None, max_iterations=5)
        state = await agent.run("What is encryption?")
        assert len(state.retrieved_docs) > 0
        assert state.current_answer != ""
        assert state.iteration >= 2  # retrieve + generate + terminate

    async def test_run_with_llm(self, mock_retrieve_fn) -> None:
        """LLM-driven loop: retrieve -> generate -> terminate."""
        llm = AsyncMockLLMProvider(responses=[
            json.dumps({"action": "retrieve", "reason": "need docs", "params": {"top_k": 3}}),
            json.dumps({"action": "generate", "reason": "have docs"}),
            json.dumps({"action": "terminate", "reason": "answer produced"}),
        ])
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=llm, max_iterations=5)
        state = await agent.run("What is encryption?")
        assert len(state.retrieved_docs) > 0
        assert state.current_answer != ""
        assert len(state.action_history) >= 3

    async def test_run_hits_max_iterations(self, mock_retrieve_fn) -> None:
        """When the LLM never picks TERMINATE, the loop stops at max_iterations."""
        llm = AsyncMockLLMProvider(responses=[
            json.dumps({"action": "retrieve", "reason": "loop", "params": {"top_k": 1}})
        ] * 10)
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=llm, max_iterations=3)
        state = await agent.run("encryption")
        assert state.iteration >= 3

    async def test_action_decision_to_dict(self) -> None:
        d = ActionDecision(action=RAGAction.GENERATE, reason="test").to_dict()
        assert d["action"] == "generate"
        assert d["reason"] == "test"

    async def test_format_state_for_llm(self, mock_retrieve_fn) -> None:
        agent = AgenticRAG(mock_retrieve_fn, llm_provider=None, max_iterations=5)
        state = AgenticRAGState(
            query="test",
            retrieved_docs=[{"doc_id": "d1", "text": "content"}],
        )
        formatted = agent.format_state_for_llm(state)
        assert "Query: test" in formatted
        assert "Documents retrieved: 1" in formatted


# ===========================================================================
# TTLCache
# ===========================================================================

class TestTTLCache:
    """Tests for :class:`TTLCache`."""

    def test_get_set_basic(self) -> None:
        cache = TTLCache(max_size=10, ttl_seconds=60)
        assert cache.get("missing") is None
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_set_overwrite(self) -> None:
        cache = TTLCache(max_size=10, ttl_seconds=60)
        cache.set("key", "v1")
        cache.set("key", "v2")
        assert cache.get("key") == "v2"

    def test_clear(self) -> None:
        cache = TTLCache(max_size=10, ttl_seconds=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert cache.get("a") is None
        assert cache.get("b") is None

    def test_stats(self) -> None:
        cache = TTLCache(max_size=10, ttl_seconds=60)
        cache.set("a", 1)
        cache.get("a")  # hit
        cache.get("missing")  # miss
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["size"] == 1
        assert stats["hit_rate"] == pytest.approx(0.5)
        assert stats["max_size"] == 10
        assert stats["ttl_seconds"] == 60

    def test_stats_empty_cache(self) -> None:
        cache = TTLCache(max_size=10, ttl_seconds=60)
        stats = cache.stats()
        assert stats["hit_rate"] == 0.0
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["evictions"] == 0

    def test_ttl_expiration(self) -> None:
        """Entries expire after their TTL."""
        cache = TTLCache(max_size=10, ttl_seconds=0.05)
        cache.set("key", "value")
        assert cache.get("key") == "value"
        time.sleep(0.1)
        assert cache.get("key") is None

    def test_ttl_expired_counts_as_eviction(self) -> None:
        cache = TTLCache(max_size=10, ttl_seconds=0.05)
        cache.set("key", "value")
        time.sleep(0.1)
        cache.get("key")  # expired -> evicted
        stats = cache.stats()
        assert stats["evictions"] >= 1

    def test_lru_eviction(self) -> None:
        """When max_size is exceeded, the LRU entry is evicted."""
        cache = TTLCache(max_size=3, ttl_seconds=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        # Access "a" to make it more recent than "b".
        cache.get("a")
        cache.set("d", 4)  # should evict "b" (LRU)
        assert cache.get("b") is None
        assert cache.get("a") == 1
        assert cache.get("c") == 3
        assert cache.get("d") == 4

    def test_lru_update_on_set_existing(self) -> None:
        """Re-setting an existing key refreshes its recency."""
        cache = TTLCache(max_size=3, ttl_seconds=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        cache.set("a", 10)  # "a" becomes most recent
        cache.set("d", 4)   # should evict "b"
        assert cache.get("a") == 10
        assert cache.get("b") is None

    def test_delete(self) -> None:
        cache = TTLCache(max_size=10, ttl_seconds=60)
        cache.set("key", "value")
        assert cache.delete("key") is True
        assert cache.get("key") is None
        assert cache.delete("key") is False

    def test_invalid_max_size(self) -> None:
        with pytest.raises(ValueError):
            TTLCache(max_size=0, ttl_seconds=60)

    def test_invalid_ttl(self) -> None:
        with pytest.raises(ValueError):
            TTLCache(max_size=10, ttl_seconds=0)


# ===========================================================================
# EmbeddingCache
# ===========================================================================

class TestEmbeddingCache:
    """Tests for :class:`EmbeddingCache`."""

    def test_get_set_embedding(self) -> None:
        cache = EmbeddingCache(max_size=10, ttl_seconds=60)
        assert cache.get_embedding("hello") is None
        embedding = [0.1, 0.2, 0.3]
        cache.set_embedding("hello", embedding)
        assert cache.get_embedding("hello") == embedding

    def test_same_text_same_key(self) -> None:
        """Identical text always maps to the same cache key."""
        cache = EmbeddingCache(max_size=10, ttl_seconds=60)
        cache.set_embedding("world", [1.0, 2.0])
        assert cache.get_embedding("world") == [1.0, 2.0]
        # Different text should miss.
        assert cache.get_embedding("worl") is None

    def test_delete(self) -> None:
        cache = EmbeddingCache(max_size=10, ttl_seconds=60)
        cache.set_embedding("text", [0.5])
        assert cache.delete("text") is True
        assert cache.get_embedding("text") is None
        assert cache.delete("text") is False

    def test_clear(self) -> None:
        cache = EmbeddingCache(max_size=10, ttl_seconds=60)
        cache.set_embedding("a", [1.0])
        cache.set_embedding("b", [2.0])
        cache.clear()
        assert cache.get_embedding("a") is None
        assert cache.get_embedding("b") is None

    def test_stats(self) -> None:
        cache = EmbeddingCache(max_size=10, ttl_seconds=60)
        cache.set_embedding("text", [1.0])
        cache.get_embedding("text")
        cache.get_embedding("missing")
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1

    def test_ttl_expiration(self) -> None:
        cache = EmbeddingCache(max_size=10, ttl_seconds=0.05)
        cache.set_embedding("text", [1.0])
        time.sleep(0.1)
        assert cache.get_embedding("text") is None


# ===========================================================================
# QueryResultCache
# ===========================================================================

class TestQueryResultCache:
    """Tests for :class:`QueryResultCache`."""

    def test_get_set_result(self) -> None:
        cache = QueryResultCache(max_size=10, ttl_seconds=60)
        assert cache.get_result("qhash1") is None
        result = {"answer": "test", "sources": [{"doc_id": "d1"}]}
        cache.set_result("qhash1", result)
        assert cache.get_result("qhash1") == result

    def test_invalidate_for_doc(self) -> None:
        """Invalidating a document drops only results that referenced it."""
        cache = QueryResultCache(max_size=10, ttl_seconds=60)
        cache.set_result("q1", {"answer": "a1", "doc_ids": ["d1", "d2"]})
        cache.set_result("q2", {"answer": "a2", "doc_ids": ["d2", "d3"]})
        cache.set_result("q3", {"answer": "a3", "doc_ids": ["d4"]})
        invalidated = cache.invalidate_for_doc("d1")
        assert invalidated == 1
        assert cache.get_result("q1") is None
        assert cache.get_result("q2") is not None
        assert cache.get_result("q3") is not None

    def test_invalidate_for_doc_via_sources(self) -> None:
        """Documents referenced via ``sources`` are also indexed."""
        cache = QueryResultCache(max_size=10, ttl_seconds=60)
        result = {
            "answer": "a1",
            "sources": [
                {"doc_id": "doc_a", "score": 0.9},
                {"doc_id": "doc_b", "score": 0.8},
            ],
        }
        cache.set_result("q1", result)
        invalidated = cache.invalidate_for_doc("doc_a")
        assert invalidated == 1
        assert cache.get_result("q1") is None

    def test_invalidate_for_doc_empty_id(self) -> None:
        cache = QueryResultCache(max_size=10, ttl_seconds=60)
        cache.set_result("q1", {"doc_ids": ["d1"]})
        assert cache.invalidate_for_doc("") == 0

    def test_invalidate_for_doc_not_indexed(self) -> None:
        cache = QueryResultCache(max_size=10, ttl_seconds=60)
        cache.set_result("q1", {"doc_ids": ["d1"]})
        assert cache.invalidate_for_doc("nonexistent") == 0

    def test_invalidate_all(self) -> None:
        cache = QueryResultCache(max_size=10, ttl_seconds=60)
        cache.set_result("q1", {"doc_ids": ["d1"]})
        cache.set_result("q2", {"doc_ids": ["d2"]})
        cache.invalidate_all()
        assert cache.get_result("q1") is None
        assert cache.get_result("q2") is None

    def test_set_result_updates_index(self) -> None:
        """Re-setting a result for the same query_hash updates its doc associations."""
        cache = QueryResultCache(max_size=10, ttl_seconds=60)
        cache.set_result("q1", {"doc_ids": ["d1"]})
        cache.set_result("q1", {"doc_ids": ["d2"]})
        # d1 should no longer be associated.
        assert cache.invalidate_for_doc("d1") == 0
        # d2 should still be associated.
        assert cache.invalidate_for_doc("d2") == 1

    def test_stats(self) -> None:
        cache = QueryResultCache(max_size=10, ttl_seconds=60)
        cache.set_result("q1", {"doc_ids": ["d1", "d2"]})
        stats = cache.stats()
        assert stats["size"] == 1
        assert stats["indexed_docs"] == 2

    def test_ttl_expiration(self) -> None:
        cache = QueryResultCache(max_size=10, ttl_seconds=0.05)
        cache.set_result("q1", {"answer": "test"})
        time.sleep(0.1)
        assert cache.get_result("q1") is None
