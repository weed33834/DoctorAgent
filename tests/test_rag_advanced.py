# mypy: ignore-errors
"""Tests for the enterprise-grade RAG enhancements.

Covers: HyDE, Step-Back, Multi-Query+RRF, SubQuestionEngine,
ResponseSynthesizer (Compact/Refine/Tree), ANN index (NumPy/LSH),
context engineering (semantic dedup / compression / importance weighting),
streaming output, evaluation integration, parent-child chunk expansion,
recursive retrieval, and RagConfig feature flags.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from doctoragent.model.embedding import DeterministicEmbeddingProvider
from doctoragent.model.rag import (
    ChunkStorage,
    ContextEngineer,
    ContextWindow,
    HybridRetriever,
    LshAnnIndex,
    NumpyAnnIndex,
    QueryTransformer,
    RagConfig,
    RagPipeline,
    RagResponse,
    ResponseSynthesizer,
    SubQuestionEngine,
    build_ann_index,
)
from doctoragent.orchestration.task_store import TaskStore


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "rag_test.db"


@pytest.fixture
def embedding_provider() -> DeterministicEmbeddingProvider:
    return DeterministicEmbeddingProvider(dimension=32)


@pytest.fixture
def task_store(db_path: Path) -> TaskStore:
    return TaskStore(db_path)


@pytest.fixture
def rag_pipeline(
    db_path: Path, embedding_provider: DeterministicEmbeddingProvider
) -> RagPipeline:
    return RagPipeline(db_path, embedding_provider=embedding_provider)


def _index_sample_chunks(
    store: TaskStore,
    provider: DeterministicEmbeddingProvider,
    texts: list[tuple[str, str, str]],
) -> None:
    """Index sample chunks: (chunk_id, task_id, text)."""
    from doctoragent.api.schemas import ClassificationResult
    from uuid import uuid4

    for chunk_id, task_id, text in texts:
        cls = ClassificationResult(
            sensitivity="low",
            category="test",
            tags=[],
            summary=text[:50],
            disguise_name="test",
            disguise_extension="log",
        )
        store.index_embedding(uuid4(), Path(f"/vault/{chunk_id}.log"), cls, provider)
        # Also store as a chunk for dense/BM25 retrieval.
        store.store_chunks(
            task_id,
            [
                {
                    "chunk_id": chunk_id,
                    "task_id": task_id,
                    "vault_path": f"/vault/{chunk_id}.log",
                    "category": "test",
                    "summary": text[:50],
                    "chunk_index": 0,
                    "text": text,
                    "start_char": 0,
                    "end_char": len(text),
                    "parent_chunk_id": None,
                }
            ],
            provider,
        )


# ---------------------------------------------------------------------------
# RagConfig
# ---------------------------------------------------------------------------

class TestRagConfig:
    """Test RagConfig defaults and feature flags."""

    def test_defaults_all_off(self) -> None:
        """Default config has all enterprise features disabled."""
        cfg = RagConfig()
        assert cfg.enable_hyde is False
        assert cfg.enable_step_back is False
        assert cfg.enable_multi_query is False
        assert cfg.enable_subquestion is False
        assert cfg.enable_semantic_dedup is False
        assert cfg.enable_context_compression is False
        assert cfg.enable_importance_weighting is False
        assert cfg.enable_rag_evaluation is False
        assert cfg.synthesis_strategy == "compact"

    def test_enable_flags(self) -> None:
        """Flags can be toggled on."""
        cfg = RagConfig(
            enable_hyde=True,
            enable_multi_query=True,
            enable_subquestion=True,
            synthesis_strategy="tree",
        )
        assert cfg.enable_hyde is True
        assert cfg.enable_multi_query is True
        assert cfg.enable_subquestion is True
        assert cfg.synthesis_strategy == "tree"


# ---------------------------------------------------------------------------
# Query Transformer — HyDE / Step-Back / Multi-Query / RRF
# ---------------------------------------------------------------------------

class TestQueryTransformerAdvanced:
    """Test HyDE, step-back, multi-query, and RRF fusion."""

    def test_hyde_without_llm_returns_query(self) -> None:
        """HyDE without LLM returns the original query."""
        qt = QueryTransformer(llm_provider=None)
        result = qt.hyde_transform("什么是合同?")
        assert result == ["什么是合同?"]

    def test_hyde_with_llm_returns_hypothetical_doc(self) -> None:
        """HyDE with LLM returns a hypothetical answer document."""
        llm = MockLLMProvider(responses=["合同是双方签署的法律文件。"])
        qt = QueryTransformer(llm_provider=llm)
        result = qt.hyde_transform("什么是合同?")
        assert len(result) == 1
        assert "法律文件" in result[0]

    def test_step_back_without_llm_returns_none(self) -> None:
        """Step-back without LLM returns None."""
        qt = QueryTransformer(llm_provider=None)
        assert qt.step_back_query("2023年Q3营收是多少?") is None

    def test_step_back_with_llm_returns_abstract_query(self) -> None:
        """Step-back with LLM returns a more abstract question."""
        llm = MockLLMProvider(responses=["公司各季度的营收情况如何?"])
        qt = QueryTransformer(llm_provider=llm)
        result = qt.step_back_query("2023年Q3营收是多少?")
        assert result is not None
        assert "营收" in result

    def test_multi_query_without_llm_returns_original(self) -> None:
        """Multi-query without LLM returns only the original query."""
        qt = QueryTransformer(llm_provider=None)
        result = qt.multi_query("合同到期日")
        assert result == ["合同到期日"]

    def test_multi_query_with_llm_returns_variations(self) -> None:
        """Multi-query with LLM returns multiple variations."""
        llm = MockLLMProvider(
            responses=[json.dumps(["合同何时到期", "合同终止日期", "合同有效期限"])]
        )
        qt = QueryTransformer(llm_provider=llm)
        result = qt.multi_query("合同到期日", n=3)
        assert len(result) >= 2  # original + at least one variation
        assert result[0] == "合同到期日"

    def test_rrf_fuse_empty_returns_empty(self) -> None:
        """RRF fusion of empty input returns empty."""
        assert QueryTransformer.rrf_fuse([]) == []
        assert QueryTransformer.rrf_fuse([[], []]) == []

    def test_rrf_fuse_single_list_returns_copy(self) -> None:
        """RRF fusion of a single list returns a copy."""
        chunks = [{"chunk_id": "a", "score": 1.0}, {"chunk_id": "b", "score": 0.5}]
        result = QueryTransformer.rrf_fuse([chunks])
        assert len(result) == 2
        assert result[0]["chunk_id"] == "a"

    def test_rrf_fuse_merges_rankings(self) -> None:
        """RRF fusion merges multiple rankings by reciprocal rank."""
        list1 = [{"chunk_id": "a", "text": "foo"}, {"chunk_id": "b", "text": "bar"}]
        list2 = [{"chunk_id": "b", "text": "bar"}, {"chunk_id": "c", "text": "baz"}]
        result = QueryTransformer.rrf_fuse([list1, list2])
        ids = [r["chunk_id"] for r in result]
        # 'b' appears in both lists at rank 1 and 0, so it should rank high.
        assert "b" in ids
        assert ids[0] == "b"  # highest fused score

    def test_rrf_fuse_respects_weights(self) -> None:
        """RRF fusion applies per-list weights."""
        list1 = [{"chunk_id": "a"}, {"chunk_id": "b"}]
        list2 = [{"chunk_id": "b"}, {"chunk_id": "a"}]
        # Weight list1 heavily.
        result = QueryTransformer.rrf_fuse([list1, list2], weights=[10.0, 1.0])
        assert result[0]["chunk_id"] == "a"  # list1's top wins


# ---------------------------------------------------------------------------
# ANN Index
# ---------------------------------------------------------------------------

class TestAnnIndex:
    """Test NumPy and LSH ANN indexes."""

    def test_numpy_index_build_and_search(self) -> None:
        """NumpyAnnIndex builds and returns sorted results."""
        idx = NumpyAnnIndex()
        vectors = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        ids = ["a", "b", "c"]
        idx.build(vectors, ids)
        assert idx.size == 3
        results = idx.search([1, 0, 0], top_k=2)
        assert len(results) == 2
        assert results[0][0] == "a"  # closest match
        assert results[0][1] > results[1][1]  # sorted by descending score

    def test_numpy_index_empty_build(self) -> None:
        """NumpyAnnIndex with empty vectors returns empty search."""
        idx = NumpyAnnIndex()
        idx.build([], [])
        assert idx.size == 0
        assert idx.search([1, 0], top_k=5) == []

    def test_numpy_index_persistence(self, tmp_path: Path) -> None:
        """NumpyAnnIndex saves and loads from disk."""
        idx = NumpyAnnIndex()
        vectors = [[1, 0, 0], [0, 1, 0]]
        ids = ["a", "b"]
        idx.build(vectors, ids)
        path = tmp_path / "index.ann"
        idx.save(path)
        loaded = NumpyAnnIndex.load(path)
        assert loaded is not None
        assert loaded.size == 2
        results = loaded.search([1, 0, 0], top_k=1)
        assert results[0][0] == "a"

    def test_numpy_index_load_nonexistent(self, tmp_path: Path) -> None:
        """Loading a non-existent file returns None."""
        assert NumpyAnnIndex.load(tmp_path / "nope.ann") is None

    def test_lsh_index_build_and_search(self) -> None:
        """LshAnnIndex builds and returns results."""
        idx = LshAnnIndex(num_hashes=8)
        vectors = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]]
        ids = ["a", "b", "c"]
        idx.build(vectors, ids)
        assert idx.size == 3
        results = idx.search([1, 0, 0, 0], top_k=3)
        # LSH is approximate, but should return at least one result.
        assert len(results) >= 1

    def test_lsh_index_persistence(self, tmp_path: Path) -> None:
        """LshAnnIndex saves and loads from disk."""
        idx = LshAnnIndex(num_hashes=8)
        vectors = [[1, 0, 0, 0], [0, 1, 0, 0]]
        ids = ["a", "b"]
        idx.build(vectors, ids)
        path = tmp_path / "lsh.ann"
        idx.save(path)
        loaded = LshAnnIndex.load(path)
        assert loaded is not None
        assert loaded.size == 2

    def test_build_ann_index_numpy_mode(self) -> None:
        """build_ann_index with numpy mode returns NumpyAnnIndex."""
        idx = build_ann_index([[1, 0], [0, 1]], ["a", "b"], mode="numpy")
        assert isinstance(idx, NumpyAnnIndex)

    def test_build_ann_index_lsh_mode(self) -> None:
        """build_ann_index with lsh mode returns LshAnnIndex."""
        idx = build_ann_index([[1, 0, 0], [0, 1, 0]], ["a", "b"], mode="lsh")
        assert isinstance(idx, LshAnnIndex)

    def test_build_ann_index_brute_returns_none(self) -> None:
        """build_ann_index with brute mode returns None."""
        assert build_ann_index([[1, 0]], ["a"], mode="brute") is None

    def test_build_ann_index_empty_returns_none(self) -> None:
        """build_ann_index with empty vectors returns None."""
        assert build_ann_index([], [], mode="numpy") is None


# ---------------------------------------------------------------------------
# Hybrid Retriever — ANN + recursive + parent-child
# ---------------------------------------------------------------------------

class TestHybridRetrieverAdvanced:
    """Test ANN index, recursive retrieval, and parent-child expansion."""

    def test_retriever_uses_brute_force_below_threshold(
        self, db_path: Path, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """Below the ANN threshold, brute-force search is used (returns None)."""
        cfg = RagConfig(enable_ann_index=True, ann_threshold=1000)
        retriever = HybridRetriever(
            db_path, embedding_provider, config=cfg
        )
        # No chunks indexed yet, so _get_ann_index returns None.
        assert retriever._get_ann_index() is None

    def test_retriever_invalidates_index(
        self, db_path: Path, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """invalidate_index clears the cached ANN index."""
        retriever = HybridRetriever(db_path, embedding_provider)
        retriever._ann_index = NumpyAnnIndex()  # type: ignore[assignment]
        retriever._ann_signature = 42
        retriever.invalidate_index()
        assert retriever._ann_index is None
        assert retriever._ann_signature == -1

    def test_recursive_doc_filter_returns_empty_without_vectors(
        self, db_path: Path, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """_recursive_doc_filter returns empty set when no doc vectors exist."""
        cfg = RagConfig(enable_recursive_retrieval=True)
        retriever = HybridRetriever(db_path, embedding_provider, config=cfg)
        result = retriever._recursive_doc_filter([1.0] * 32)
        assert result == set()

    def test_parent_child_expansion_disabled_by_default(
        self, db_path: Path, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """expand_to_parents is a no-op when parent_child is disabled."""
        retriever = HybridRetriever(db_path, embedding_provider)
        chunks = [{"chunk_id": "a", "text": "hello"}]
        result = retriever.expand_to_parents(chunks)
        assert result == chunks

    def test_parent_child_expansion_no_parent(
        self, db_path: Path, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """expand_to_parents returns chunks unchanged when no parent exists."""
        cfg = RagConfig(enable_parent_child=True)
        retriever = HybridRetriever(db_path, embedding_provider, config=cfg)
        chunks = [{"chunk_id": "nonexistent", "text": "hello"}]
        result = retriever.expand_to_parents(chunks)
        assert len(result) == 1
        assert result[0]["text"] == "hello"


# ---------------------------------------------------------------------------
# Context Engineering — semantic dedup / compression / importance weighting
# ---------------------------------------------------------------------------

class TestContextEngineeringAdvanced:
    """Test semantic dedup, context compression, importance weighting."""

    def test_semantic_dedup_without_provider_returns_all(
        self, tmp_path: Path
    ) -> None:
        """Semantic dedup without embedding provider returns all chunks."""
        engineer = ContextEngineer(
            config=RagConfig(enable_semantic_dedup=True)
        )
        chunks = [{"text": "a"}, {"text": "b"}]
        result = engineer._semantic_dedup(chunks)
        assert len(result) == 2

    def test_semantic_dedup_with_provider(
        self, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """Semantic dedup removes near-duplicate chunks."""
        engineer = ContextEngineer(
            embedding_provider=embedding_provider,
            config=RagConfig(enable_semantic_dedup=True, semantic_dedup_threshold=0.0),
        )
        # With threshold 0.0, every chunk is a "duplicate" of the first.
        chunks = [{"text": "hello"}, {"text": "world"}]
        result = engineer._semantic_dedup(chunks)
        assert len(result) == 1  # only the first survives

    def test_importance_weighting_orders_by_priority(self) -> None:
        """Importance weighting puts high-importance chunks first."""
        engineer = ContextEngineer(
            config=RagConfig(enable_importance_weighting=True)
        )
        chunks = [
            {"text": "low", "score": 0.9, "importance": 0.1},
            {"text": "high", "score": 0.1, "importance": 0.9},
        ]
        result = engineer._apply_importance_weighting(chunks)
        assert result[0]["text"] == "high"

    def test_compress_context_without_llm_returns_original(self) -> None:
        """Compression without LLM returns the original text."""
        engineer = ContextEngineer(
            config=RagConfig(enable_context_compression=True)
        )
        text = "x" * 10000  # large enough to trigger
        result = engineer._compress_context(text, "q", budget=1000)
        assert result == text

    def test_compress_context_under_threshold_returns_original(
        self, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """Compression is not triggered for small context."""
        llm = MockLLMProvider(responses=["compressed"])
        engineer = ContextEngineer(
            llm_provider=llm,
            config=RagConfig(enable_context_compression=True),
        )
        text = "short text"
        result = engineer._compress_context(text, "q", budget=1000)
        assert result == "short text"  # under threshold


# ---------------------------------------------------------------------------
# Response Synthesizer
# ---------------------------------------------------------------------------

class TestResponseSynthesizer:
    """Test Compact / Refine / Tree-Summarize strategies."""

    def test_select_strategy_auto_compact(self) -> None:
        """Auto selects Compact for <= 3 chunks."""
        assert ResponseSynthesizer.select_strategy(1) == "compact"
        assert ResponseSynthesizer.select_strategy(3) == "compact"

    def test_select_strategy_auto_refine(self) -> None:
        """Auto selects Refine for 4-8 chunks."""
        assert ResponseSynthesizer.select_strategy(4) == "refine"
        assert ResponseSynthesizer.select_strategy(8) == "refine"

    def test_select_strategy_auto_tree(self) -> None:
        """Auto selects Tree for > 8 chunks."""
        assert ResponseSynthesizer.select_strategy(9) == "tree"
        assert ResponseSynthesizer.select_strategy(100) == "tree"

    def test_select_strategy_explicit_override(self) -> None:
        """Explicit strategy overrides auto-selection."""
        assert ResponseSynthesizer.select_strategy(1, "tree") == "tree"
        assert ResponseSynthesizer.select_strategy(100, "compact") == "compact"

    def test_compact_without_llm(self) -> None:
        """Compact without LLM uses fallback context."""
        synth = ResponseSynthesizer(llm_provider=None)
        ctx = ContextWindow(
            system_prompt="sys",
            retrieved_context="retrieved info here",
        )
        result = synth.synthesize("q?", [{"text": "a"}], ctx, strategy="compact")
        assert "retrieved info" in result

    def test_refine_without_llm_falls_back_to_compact(self) -> None:
        """Refine without LLM degrades to compact."""
        synth = ResponseSynthesizer(llm_provider=None)
        ctx = ContextWindow(retrieved_context="info")
        result = synth.synthesize(
            "q?", [{"text": "a"}, {"text": "b"}], ctx, strategy="refine"
        )
        assert result  # non-empty

    def test_tree_without_llm_falls_back_to_compact(self) -> None:
        """Tree without LLM degrades to compact."""
        synth = ResponseSynthesizer(llm_provider=None)
        ctx = ContextWindow(retrieved_context="info")
        result = synth.synthesize(
            "q?", [{"text": "a"}, {"text": "b"}], ctx, strategy="tree"
        )
        assert result

    def test_refine_with_llm(self) -> None:
        """Refine with LLM generates and refines iteratively."""
        llm = MockLLMProvider(
            responses=["initial answer", "refined answer"]
        )
        synth = ResponseSynthesizer(llm_provider=llm)
        ctx = ContextWindow(retrieved_context="info")
        result = synth.synthesize(
            "q?", [{"text": "chunk1"}, {"text": "chunk2"}], ctx, strategy="refine"
        )
        assert "refined" in result

    def test_tree_with_llm(self) -> None:
        """Tree summarize with LLM merges pairwise."""
        llm = MockLLMProvider(
            responses=["leaf1", "leaf2", "merged"]
        )
        synth = ResponseSynthesizer(llm_provider=llm)
        ctx = ContextWindow(retrieved_context="info")
        result = synth.synthesize(
            "q?", [{"text": "a"}, {"text": "b"}], ctx, strategy="tree"
        )
        assert result  # non-empty

    def test_synthesize_empty_chunks_returns_empty(self) -> None:
        """Synthesize with no chunks returns empty string."""
        synth = ResponseSynthesizer(llm_provider=None)
        ctx = ContextWindow()
        assert synth.synthesize("q?", [], ctx) == ""

    def test_bind_pipeline_sets_reference(self, rag_pipeline: RagPipeline) -> None:
        """bind_pipeline sets the pipeline reference."""
        synth = ResponseSynthesizer()
        synth.bind_pipeline(rag_pipeline)
        assert synth._pipeline is rag_pipeline


# ---------------------------------------------------------------------------
# Sub-Question Engine
# ---------------------------------------------------------------------------

class TestSubQuestionEngine:
    """Test sub-question decomposition and multi-hop reasoning."""

    def test_should_decompose_without_llm_returns_false(
        self, rag_pipeline: RagPipeline
    ) -> None:
        """Without LLM, decomposition is never triggered."""
        engine = SubQuestionEngine(rag_pipeline)
        assert engine.should_decompose("complex question") is False

    def test_should_decompose_with_llm_yes(
        self, db_path: Path, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """LLM says yes -> should_decompose returns True."""
        llm = MockLLMProvider(responses=["是"])
        pipeline = RagPipeline(db_path, embedding_provider=embedding_provider, llm_provider=llm)
        engine = SubQuestionEngine(pipeline)
        assert engine.should_decompose("对比A和B的区别并总结") is True

    def test_should_decompose_with_llm_no(
        self, db_path: Path, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """LLM says no -> should_decompose returns False."""
        llm = MockLLMProvider(responses=["否"])
        pipeline = RagPipeline(db_path, embedding_provider=embedding_provider, llm_provider=llm)
        engine = SubQuestionEngine(pipeline)
        assert engine.should_decompose("简单问题") is False

    def test_decompose_without_llm_returns_empty(
        self, rag_pipeline: RagPipeline
    ) -> None:
        """Decompose without LLM returns empty list."""
        engine = SubQuestionEngine(rag_pipeline)
        assert engine.decompose("any question") == []

    def test_answer_without_decomposition_returns_empty(
        self, rag_pipeline: RagPipeline
    ) -> None:
        """answer() returns empty string when no decomposition is possible."""
        engine = SubQuestionEngine(rag_pipeline)
        assert engine.answer("simple question") == ""

    def test_merge_final_without_llm_concatenates(
        self, rag_pipeline: RagPipeline
    ) -> None:
        """_merge_final without LLM concatenates sub-answers."""
        engine = SubQuestionEngine(rag_pipeline)
        result = engine._merge_final(
            "original", ["sub1", "sub2"], ["ans1", "ans2"]
        )
        assert "ans1" in result
        assert "ans2" in result


# ---------------------------------------------------------------------------
# RagPipeline — integration with config flags
# ---------------------------------------------------------------------------

class TestRagPipelineAdvanced:
    """Test RagPipeline with enterprise features enabled."""

    def test_pipeline_with_config(
        self, db_path: Path, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """Pipeline accepts a RagConfig and wires it to subsystems."""
        cfg = RagConfig(
            enable_hyde=True,
            enable_multi_query=True,
            enable_semantic_dedup=True,
        )
        pipeline = RagPipeline(
            db_path, embedding_provider=embedding_provider, config=cfg
        )
        assert pipeline.config.enable_hyde is True
        assert pipeline.context_engineer.config.enable_semantic_dedup is True
        assert pipeline.retriever.config.enable_ann_index is True
        assert pipeline.synthesizer is not None
        assert pipeline.subquestion_engine is not None

    def test_pipeline_ask_default_behavior_unchanged(
        self, rag_pipeline: RagPipeline
    ) -> None:
        """Default config (all off) produces the legacy no-results response."""
        response = rag_pipeline.ask("test", use_memory=False)
        assert isinstance(response, RagResponse)
        assert "抱歉" in response.answer or "未找到" in response.answer
        assert response.synthesis_strategy == "compact"
        assert response.evaluation_metrics == {}

    def test_pipeline_ask_with_evaluation_enabled(
        self,
        db_path: Path,
        embedding_provider: DeterministicEmbeddingProvider,
    ) -> None:
        """When evaluation is enabled, metrics are populated."""
        cfg = RagConfig(enable_rag_evaluation=True)
        pipeline = RagPipeline(
            db_path, embedding_provider=embedding_provider, config=cfg
        )
        response = pipeline.ask("test", use_memory=False)
        # Even with no chunks, evaluation still runs (returns empty dict on
        # failure or metrics with 0 scores).
        assert isinstance(response.evaluation_metrics, dict)

    def test_pipeline_model_name_without_llm(self, rag_pipeline: RagPipeline) -> None:
        """_model_name returns 'none' without LLM provider."""
        assert rag_pipeline._model_name() == "none"

    def test_pipeline_model_name_with_llm(
        self, db_path: Path, embedding_provider: DeterministicEmbeddingProvider
    ) -> None:
        """_model_name extracts the model name from the provider."""
        llm = MockLLMProvider()
        pipeline = RagPipeline(
            db_path, embedding_provider=embedding_provider, llm_provider=llm
        )
        assert pipeline._model_name() == "mock-model"

    def test_pipeline_ask_with_hyde_override(
        self,
        db_path: Path,
        embedding_provider: DeterministicEmbeddingProvider,
    ) -> None:
        """ask() with use_hyde=True triggers HyDE even when config is off."""
        llm = MockLLMProvider(responses=["hypothetical answer doc"])
        pipeline = RagPipeline(
            db_path, embedding_provider=embedding_provider, llm_provider=llm
        )
        # Should not crash; HyDE generates a doc, retrieves (empty), returns
        # no-results response.
        response = pipeline.ask("test question", use_memory=False, use_hyde=True)
        assert isinstance(response, RagResponse)
        assert llm.call_count >= 1  # HyDE LLM call was made

    def test_pipeline_ask_with_subquestion_override(
        self,
        db_path: Path,
        embedding_provider: DeterministicEmbeddingProvider,
    ) -> None:
        """ask() with use_subquestion=True checks decomposition."""
        llm = MockLLMProvider(responses=["否"])  # LLM says no decomposition
        pipeline = RagPipeline(
            db_path, embedding_provider=embedding_provider, llm_provider=llm
        )
        response = pipeline.ask("simple question", use_memory=False, use_subquestion=True)
        assert isinstance(response, RagResponse)
        # should_decompose was called, returned False, so normal path ran.
        assert llm.call_count >= 1


# ---------------------------------------------------------------------------
# Streaming output
# ---------------------------------------------------------------------------

class TestStreamingOutput:
    """Test ask_stream async generator."""

    def test_ask_stream_yields_events(
        self, rag_pipeline: RagPipeline
    ) -> None:
        """ask_stream yields status, retrieved, token, and done events."""
        events = asyncio.run(self._collect(rag_pipeline))

        types = [e["type"] for e in events]
        assert "status" in types
        assert "retrieved" in types
        assert "token" in types
        assert "done" in types

        # The last event should be 'done' with a RagResponse.
        done_event = events[-1]
        assert done_event["type"] == "done"
        assert isinstance(done_event["response"], RagResponse)

    def test_ask_stream_no_data_yields_tokens(
        self, rag_pipeline: RagPipeline
    ) -> None:
        """ask_stream with no data still yields token events for the message."""
        events = asyncio.run(self._collect(rag_pipeline, use_memory=False))
        token_events = [e for e in events if e["type"] == "token"]
        assert len(token_events) > 0

    @staticmethod
    async def _collect(
        pipeline: RagPipeline, **kwargs: Any
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async for event in pipeline.ask_stream("test question", **kwargs):
            events.append(event)
        return events


# ---------------------------------------------------------------------------
# ChunkStorage — parent_chunk_id
# ---------------------------------------------------------------------------

class TestChunkStorageParentChild:
    """Test ChunkStorage with parent_chunk_id support."""

    def test_store_and_retrieve_parent_chunk_id(
        self, db_path: Path, task_store: TaskStore
    ) -> None:
        """store_chunk persists parent_chunk_id and get_chunks_by_task returns it."""
        store = ChunkStorage(db_path)
        # Store a parent chunk.
        store.store_chunk({
            "chunk_id": "parent_1",
            "task_id": "task_1",
            "text": "parent text",
        })
        # Store a child chunk referencing the parent.
        store.store_chunk({
            "chunk_id": "child_1",
            "task_id": "task_1",
            "text": "child text",
            "parent_chunk_id": "parent_1",
        })

        chunks = store.get_chunks_by_task("task_1")
        assert len(chunks) == 2
        child = next(c for c in chunks if c["chunk_id"] == "child_1")
        assert child["parent_chunk_id"] == "parent_1"
        parent = next(c for c in chunks if c["chunk_id"] == "parent_1")
        assert parent["parent_chunk_id"] is None

    def test_store_chunk_without_parent_chunk_id(
        self, db_path: Path, task_store: TaskStore
    ) -> None:
        """Chunks without parent_chunk_id store NULL."""
        store = ChunkStorage(db_path)
        store.store_chunk({
            "chunk_id": "orphan_1",
            "task_id": "task_2",
            "text": "orphan text",
        })
        chunks = store.get_chunks_by_task("task_2")
        assert len(chunks) == 1
        assert chunks[0]["parent_chunk_id"] is None


# ---------------------------------------------------------------------------
# RagResponse — new fields
# ---------------------------------------------------------------------------

class TestRagResponseAdvanced:
    """Test RagResponse with evaluation_metrics and synthesis_strategy."""

    def test_response_has_evaluation_metrics_field(self) -> None:
        """RagResponse has an evaluation_metrics field defaulting to empty dict."""
        resp = RagResponse(answer="a", question="q")
        assert resp.evaluation_metrics == {}

    def test_response_has_synthesis_strategy_field(self) -> None:
        """RagResponse has a synthesis_strategy field defaulting to compact."""
        resp = RagResponse(answer="a", question="q")
        assert resp.synthesis_strategy == "compact"

    def test_response_with_evaluation_metrics(self) -> None:
        """RagResponse can carry evaluation metrics."""
        resp = RagResponse(
            answer="a",
            question="q",
            evaluation_metrics={"rag_score": 0.85, "faithfulness": 0.9},
            synthesis_strategy="refine",
        )
        assert resp.evaluation_metrics["rag_score"] == 0.85
        assert resp.synthesis_strategy == "refine"
