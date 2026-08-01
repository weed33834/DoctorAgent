"""Test RAG pipeline with context engineering and memory."""

import tempfile
from pathlib import Path

import pytest

from doctoragent.model.rag import (
    ChunkStorage,
    ContextEngineer,
    ConversationMemory,
    ConversationTurn,
    HybridRetriever,
    MemoryEntry,
    MemorySystem,
    QueryTransformer,
    RagPipeline,
    RagResponse,
    Reranker,
    SemanticChunker,
)


class TestMemorySystem:
    """Test memory system functionality."""

    def test_store_and_recall_facts(self, tmp_path):
        """Test storing and recalling facts."""
        db_path = tmp_path / "test.db"
        memory = MemorySystem(db_path)

        # Store facts
        memory_id1 = memory.store_fact("用户喜欢PDF文件", importance=0.8)
        memory_id2 = memory.store_fact("用户有合同文档", importance=0.6)

        # Recall facts
        facts = memory.recall_facts("PDF", limit=5)
        assert len(facts) >= 1
        assert any("PDF" in f.content for f in facts)

    def test_store_and_recall_episodes(self, tmp_path):
        """Test storing and recalling episodes."""
        db_path = tmp_path / "test.db"
        memory = MemorySystem(db_path)

        # Create session and add turns
        session_id = memory.create_session()
        memory.add_turn(session_id, "user", "我的合同在哪里？")
        memory.add_turn(session_id, "assistant", "您的合同在保险库中。")

        # Store episode
        memory.store_episode(
            session_id=session_id,
            user_message="我的合同在哪里？",
            assistant_response="您的合同在保险库中。",
            key_facts=["合同", "保险库"],
        )

        # Recall episodes
        episodes = memory.recall_episodes("合同", limit=3)
        assert len(episodes) >= 1

    def test_conversation_management(self, tmp_path):
        """Test conversation turn management."""
        db_path = tmp_path / "test.db"
        memory = MemorySystem(db_path)

        session_id = memory.create_session()
        memory.add_turn(session_id, "user", "Hello")
        memory.add_turn(session_id, "assistant", "Hi there!")
        memory.add_turn(session_id, "user", "How are you?")

        history = memory.get_conversation_history(session_id, max_turns=5)
        assert len(history.turns) == 3
        assert history.turns[0].role == "user"
        assert history.turns[1].role == "assistant"


class TestContextEngineer:
    """Test context engineering functionality."""

    def test_build_context_basic(self, tmp_path):
        """Test basic context building."""
        db_path = tmp_path / "test.db"
        memory = MemorySystem(db_path)
        engineer = ContextEngineer(memory)

        chunks = [
            {"text": "Test chunk", "vault_path": "test.txt", "category": "test"}
        ]

        context = engineer.build_context(
            question="test question",
            retrieved_chunks=chunks,
            include_memory=False,
        )

        assert context.system_prompt
        assert context.retrieved_context
        assert context.user_query == "test question"

    def test_build_context_with_memory(self, tmp_path):
        """Test context building with memory."""
        db_path = tmp_path / "test.db"
        memory = MemorySystem(db_path)
        engineer = ContextEngineer(memory)

        # Store some facts
        memory.store_fact("用户喜欢简洁回答")

        session_id = memory.create_session()
        memory.add_turn(session_id, "user", "你好")

        context = engineer.build_context(
            question="test question",
            retrieved_chunks=[],
            session_id=session_id,
            include_memory=True,
        )

        assert context.memory_used
        assert context.conversation_history

    def test_token_estimation(self, tmp_path):
        """Test token estimation."""
        engineer = ContextEngineer()
        tokens = engineer.estimate_tokens("Hello world")
        assert tokens > 0


class TestQueryTransformer:
    """Test query transformation."""

    def test_expand_query_without_llm(self):
        """Test query expansion without LLM."""
        transformer = QueryTransformer(llm_provider=None)
        queries = transformer.expand_query("test query")
        assert len(queries) == 1
        assert queries[0] == "test query"


class TestSemanticChunker:
    """Test semantic chunking."""

    def test_chunk_text(self):
        """Test text chunking."""
        chunker = SemanticChunker(chunk_size=100, chunk_overlap=20)
        text = "This is a test. It should be chunked. Each part matters."
        chunks = chunker.chunk_text(text)
        assert len(chunks) > 0
        assert all("text" in c for c in chunks)


class TestRagPipeline:
    """Test RAG pipeline integration."""

    def test_rag_initialization(self, tmp_path):
        """Test RAG pipeline initialization."""
        db_path = tmp_path / "test.db"
        rag = RagPipeline(db_path)
        assert rag.memory is not None
        assert rag.context_engineer is not None

    def test_rag_ask_without_data(self, tmp_path):
        """Test RAG ask without data."""
        db_path = tmp_path / "test.db"
        rag = RagPipeline(db_path)
        response = rag.ask("test question", use_memory=False)
        assert isinstance(response, RagResponse)
        assert "抱歉" in response.answer or "未找到" in response.answer

    def test_rag_ask_with_session(self, tmp_path):
        """Test RAG ask with session."""
        db_path = tmp_path / "test.db"
        rag = RagPipeline(db_path)

        response1 = rag.ask("First question", use_memory=True)
        assert response1.conversation_turns >= 0

        # Use same session for continuity
        if hasattr(response1, 'session_id'):
            response2 = rag.ask(
                "Follow up",
                session_id=response1.session_id if hasattr(response1, 'session_id') else None,
                use_memory=True,
            )


class TestRagResponse:
    """Test RagResponse model."""

    def test_response_model(self):
        """Test response model creation."""
        response = RagResponse(
            answer="Test answer",
            question="Test question",
            sources=[],
            model_used="test",
            retrieval_method="hybrid",
            total_chunks_searched=10,
            context_tokens_used=100,
            memory_used=True,
            conversation_turns=3,
        )
        assert response.answer == "Test answer"
        assert response.memory_used


class TestConversationMemory:
    """Test ConversationMemory model."""

    def test_conversation_memory(self):
        """Test conversation memory creation."""
        turns = [
            ConversationTurn(role="user", content="Hello"),
            ConversationTurn(role="assistant", content="Hi!"),
        ]
        memory = ConversationMemory(
            turns=turns,
            summary="Greeting exchange",
            total_tokens=20,
        )
        assert len(memory.turns) == 2
        assert memory.summary == "Greeting exchange"


class TestMemoryEntry:
    """Test MemoryEntry model."""

    def test_memory_entry(self):
        """Test memory entry creation."""
        entry = MemoryEntry(
            memory_id="test_123",
            content="User prefers PDF files",
            memory_type="semantic",
            importance=0.8,
        )
        assert entry.memory_id == "test_123"
        assert entry.importance == 0.8


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
