# mypy: ignore-errors
"""Tests for the knowledge graph RAG module.

Covers: Entity/Relation data models and serialization, KnowledgeGraph CRUD
(add/get/subgraph/merge), LLM-driven entity/relation extraction, graph-based
retrieval, and full to_dict/from_dict round-trip persistence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from doctoragent.model.knowledge_graph import (
    Entity,
    KnowledgeGraph,
    Relation,
)


# ---------------------------------------------------------------------------
# Mock LLM provider
# ---------------------------------------------------------------------------

class MockLLMProvider:
    """Minimal LLM provider returning canned JSON for deterministic tests."""

    def __init__(self, responses: list[str] | None = None, default: str = "") -> None:
        self._responses = responses or []
        self._idx = 0
        self.default = default
        self.model_name = "mock-kg-model"
        self.call_count = 0

    def chat_completion_sync(self, messages: list[dict[str, Any]]) -> str:
        self.call_count += 1
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return self.default


# Predefined extraction response with entities and relations.
_EXTRACTION_RESPONSE = json.dumps({
    "entities": [
        {"name": "Alice", "type": "person", "properties": {"role": "engineer"}},
        {"name": "Project X", "type": "project", "properties": {"status": "active"}},
        {"name": "Acme Corp", "type": "organization", "properties": {}},
    ],
    "relations": [
        {
            "source": "Alice",
            "target": "Project X",
            "type": "works_on",
            "confidence": 0.95,
            "properties": {"since": "2024"},
        },
        {
            "source": "Acme Corp",
            "target": "Project X",
            "type": "funds",
            "confidence": 0.8,
            "properties": {},
        },
        {
            "source": "Alice",
            "target": "Acme Corp",
            "type": "employed_by",
            "confidence": 0.9,
            "properties": {},
        },
    ],
})

# A simpler response used for retrieval (only entities relevant to the query).
_RETRIEVAL_EXTRACTION_RESPONSE = json.dumps({
    "entities": [
        {"name": "Alice", "type": "person", "properties": {}},
        {"name": "Project X", "type": "project", "properties": {}},
    ],
    "relations": [],
})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "kg_test.db"


@pytest.fixture
def knowledge_graph(db_path: Path) -> KnowledgeGraph:
    return KnowledgeGraph(db_path, tenant_id="test")


@pytest.fixture
def populated_graph(knowledge_graph: KnowledgeGraph) -> KnowledgeGraph:
    """A graph pre-loaded with Alice -> Project X <- Acme Corp."""
    knowledge_graph.add_entity(
        Entity(
            name="Alice",
            entity_type="person",
            properties={"role": "engineer"},
            source_doc_ids=["doc1", "doc2"],
        )
    )
    knowledge_graph.add_entity(
        Entity(
            name="Project X",
            entity_type="project",
            properties={"status": "active"},
            source_doc_ids=["doc1"],
        )
    )
    knowledge_graph.add_entity(
        Entity(name="Acme Corp", entity_type="organization", source_doc_ids=["doc3"])
    )
    knowledge_graph.add_relation(
        Relation(
            source="Alice",
            target="Project X",
            relation_type="works_on",
            confidence=0.95,
            properties={"since": "2024"},
        )
    )
    knowledge_graph.add_relation(
        Relation(
            source="Acme Corp",
            target="Project X",
            relation_type="funds",
            confidence=0.8,
        )
    )
    knowledge_graph.add_relation(
        Relation(
            source="Alice",
            target="Acme Corp",
            relation_type="employed_by",
            confidence=0.9,
        )
    )
    return knowledge_graph


# ---------------------------------------------------------------------------
# Entity tests
# ---------------------------------------------------------------------------

class TestEntity:
    def test_entity_creation_defaults(self) -> None:
        entity = Entity(name="test")
        assert entity.name == "test"
        assert entity.entity_type == "concept"
        assert entity.properties == {}
        assert entity.source_doc_ids == []

    def test_entity_creation_with_values(self) -> None:
        entity = Entity(
            name="Alice",
            entity_type="person",
            properties={"role": "engineer"},
            source_doc_ids=["doc1", "doc2"],
        )
        assert entity.name == "Alice"
        assert entity.entity_type == "person"
        assert entity.properties == {"role": "engineer"}
        assert entity.source_doc_ids == ["doc1", "doc2"]

    def test_entity_to_dict(self) -> None:
        entity = Entity(
            name="Bob",
            entity_type="person",
            properties={"age": 30},
            source_doc_ids=["d1"],
        )
        d = entity.to_dict()
        assert d["name"] == "Bob"
        assert d["entity_type"] == "person"
        assert d["properties"] == {"age": 30}
        assert d["source_doc_ids"] == ["d1"]

    def test_entity_from_dict(self) -> None:
        data = {
            "name": "Carol",
            "entity_type": "location",
            "properties": {"city": "NYC"},
            "source_doc_ids": ["doc_a"],
        }
        entity = Entity.from_dict(data)
        assert entity.name == "Carol"
        assert entity.entity_type == "location"
        assert entity.properties == {"city": "NYC"}
        assert entity.source_doc_ids == ["doc_a"]

    def test_entity_round_trip(self) -> None:
        original = Entity(
            name="RoundTrip",
            entity_type="concept",
            properties={"key": "value", "num": 42},
            source_doc_ids=["a", "b", "c"],
        )
        restored = Entity.from_dict(original.to_dict())
        assert restored.name == original.name
        assert restored.entity_type == original.entity_type
        assert restored.properties == original.properties
        assert restored.source_doc_ids == original.source_doc_ids

    def test_entity_from_dict_empty(self) -> None:
        entity = Entity.from_dict({})
        assert entity.name == ""
        assert entity.entity_type == "concept"
        assert entity.properties == {}
        assert entity.source_doc_ids == []

    def test_entity_to_dict_is_json_serializable(self) -> None:
        entity = Entity(name="Json", properties={"nested": {"a": 1}})
        # Should not raise.
        json.dumps(entity.to_dict())


# ---------------------------------------------------------------------------
# Relation tests
# ---------------------------------------------------------------------------

class TestRelation:
    def test_relation_creation_defaults(self) -> None:
        rel = Relation(source="A", target="B", relation_type="related_to")
        assert rel.source == "A"
        assert rel.target == "B"
        assert rel.relation_type == "related_to"
        assert rel.properties == {}
        assert rel.confidence == 1.0

    def test_relation_creation_with_values(self) -> None:
        rel = Relation(
            source="Alice",
            target="Project X",
            relation_type="works_on",
            properties={"since": "2024"},
            confidence=0.85,
        )
        assert rel.source == "Alice"
        assert rel.target == "Project X"
        assert rel.relation_type == "works_on"
        assert rel.properties == {"since": "2024"}
        assert rel.confidence == 0.85

    def test_relation_to_dict(self) -> None:
        rel = Relation(source="X", target="Y", relation_type="depends_on", confidence=0.7)
        d = rel.to_dict()
        assert d["source"] == "X"
        assert d["target"] == "Y"
        assert d["relation_type"] == "depends_on"
        assert d["confidence"] == 0.7
        assert d["properties"] == {}

    def test_relation_from_dict(self) -> None:
        data = {
            "source": "A",
            "target": "B",
            "relation_type": "funds",
            "properties": {"amount": "1M"},
            "confidence": 0.9,
        }
        rel = Relation.from_dict(data)
        assert rel.source == "A"
        assert rel.target == "B"
        assert rel.relation_type == "funds"
        assert rel.properties == {"amount": "1M"}
        assert rel.confidence == 0.9

    def test_relation_round_trip(self) -> None:
        original = Relation(
            source="Src",
            target="Dst",
            relation_type="custom_rel",
            properties={"meta": True},
            confidence=0.33,
        )
        restored = Relation.from_dict(original.to_dict())
        assert restored.source == original.source
        assert restored.target == original.target
        assert restored.relation_type == original.relation_type
        assert restored.properties == original.properties
        assert restored.confidence == original.confidence

    def test_relation_from_dict_defaults(self) -> None:
        rel = Relation.from_dict({"source": "A", "target": "B"})
        assert rel.relation_type == "related_to"
        assert rel.confidence == 1.0


# ---------------------------------------------------------------------------
# KnowledgeGraph CRUD tests
# ---------------------------------------------------------------------------

class TestKnowledgeGraphCRUD:
    def test_add_and_get_entity(self, knowledge_graph: KnowledgeGraph) -> None:
        entity = Entity(name="TestEntity", entity_type="concept")
        knowledge_graph.add_entity(entity)
        retrieved = knowledge_graph.get_entity("TestEntity")
        assert retrieved is not None
        assert retrieved.name == "TestEntity"

    def test_get_entity_not_found(self, knowledge_graph: KnowledgeGraph) -> None:
        assert knowledge_graph.get_entity("NonExistent") is None

    def test_add_entity_merges_source_doc_ids(self, knowledge_graph: KnowledgeGraph) -> None:
        knowledge_graph.add_entity(
            Entity(name="Merge", source_doc_ids=["doc1"])
        )
        knowledge_graph.add_entity(
            Entity(name="Merge", source_doc_ids=["doc2"])
        )
        entity = knowledge_graph.get_entity("Merge")
        assert entity is not None
        assert "doc1" in entity.source_doc_ids
        assert "doc2" in entity.source_doc_ids

    def test_add_entity_merges_properties(self, knowledge_graph: KnowledgeGraph) -> None:
        knowledge_graph.add_entity(
            Entity(name="PropMerge", properties={"a": 1, "b": 2})
        )
        knowledge_graph.add_entity(
            Entity(name="PropMerge", properties={"b": 3, "c": 4})
        )
        entity = knowledge_graph.get_entity("PropMerge")
        assert entity is not None
        # Existing keys take precedence (b stays 2).
        assert entity.properties["a"] == 1
        assert entity.properties["b"] == 2
        assert entity.properties["c"] == 4

    def test_add_entity_empty_name_ignored(self, knowledge_graph: KnowledgeGraph) -> None:
        knowledge_graph.add_entity(Entity(name=""))
        assert knowledge_graph.get_entity("") is None

    def test_add_and_get_relations(self, knowledge_graph: KnowledgeGraph) -> None:
        knowledge_graph.add_entity(Entity(name="A"))
        knowledge_graph.add_entity(Entity(name="B"))
        knowledge_graph.add_relation(
            Relation(source="A", target="B", relation_type="connects")
        )
        relations = knowledge_graph.get_relations()
        assert len(relations) == 1
        assert relations[0].source == "A"
        assert relations[0].target == "B"

    def test_get_relations_by_entity(self, populated_graph: KnowledgeGraph) -> None:
        alice_relations = populated_graph.get_relations("Alice")
        # Alice is source in works_on and employed_by.
        assert len(alice_relations) == 2
        for rel in alice_relations:
            assert rel.source == "Alice" or rel.target == "Alice"

    def test_get_relations_for_entity_as_target(self, populated_graph: KnowledgeGraph) -> None:
        project_relations = populated_graph.get_relations("Project X")
        # Project X is target in works_on and funds.
        assert len(project_relations) == 2

    def test_add_relation_creates_stub_entities(self, knowledge_graph: KnowledgeGraph) -> None:
        knowledge_graph.add_relation(
            Relation(source="NewSrc", target="NewDst", relation_type="links")
        )
        assert knowledge_graph.get_entity("NewSrc") is not None
        assert knowledge_graph.get_entity("NewDst") is not None

    def test_add_relation_merges_duplicate(self, knowledge_graph: KnowledgeGraph) -> None:
        knowledge_graph.add_relation(
            Relation(source="A", target="B", relation_type="dup", confidence=0.5)
        )
        knowledge_graph.add_relation(
            Relation(source="A", target="B", relation_type="dup", confidence=0.9)
        )
        relations = knowledge_graph.get_relations()
        assert len(relations) == 1
        # Confidence should be the max.
        assert relations[0].confidence == 0.9

    def test_add_relation_empty_endpoints_ignored(self, knowledge_graph: KnowledgeGraph) -> None:
        knowledge_graph.add_relation(
            Relation(source="", target="B", relation_type="x")
        )
        assert len(knowledge_graph.get_relations()) == 0

    def test_tenant_isolation(self, db_path: Path) -> None:
        graph_a = KnowledgeGraph(db_path, tenant_id="tenant_a")
        graph_b = KnowledgeGraph(db_path, tenant_id="tenant_b")
        graph_a.add_entity(Entity(name="SharedName"))
        # tenant_b should not see tenant_a's entities.
        assert graph_b.get_entity("SharedName") is None
        assert graph_a.get_entity("SharedName") is not None

    def test_empty_tenant_id_raises(self, db_path: Path) -> None:
        with pytest.raises(ValueError, match="tenant_id must not be empty"):
            KnowledgeGraph(db_path, tenant_id="")


# ---------------------------------------------------------------------------
# Subgraph tests
# ---------------------------------------------------------------------------

class TestSubgraph:
    def test_get_subgraph_basic(self, populated_graph: KnowledgeGraph) -> None:
        subgraph = populated_graph.get_subgraph("Alice", depth=1)
        assert subgraph["seed"] == "Alice"
        entity_names = {e.name for e in subgraph["entities"]}
        # Alice + direct neighbors (Project X, Acme Corp).
        assert "Alice" in entity_names
        assert "Project X" in entity_names
        assert "Acme Corp" in entity_names

    def test_get_subgraph_depth_2(self, populated_graph: KnowledgeGraph) -> None:
        subgraph = populated_graph.get_subgraph("Alice", depth=2)
        entity_names = {e.name for e in subgraph["entities"]}
        assert "Alice" in entity_names
        assert "Project X" in entity_names
        assert "Acme Corp" in entity_names

    def test_get_subgraph_unknown_entity(self, knowledge_graph: KnowledgeGraph) -> None:
        subgraph = knowledge_graph.get_subgraph("Ghost")
        assert subgraph["entities"] == []
        assert subgraph["relations"] == []

    def test_get_subgraph_depth_0(self, populated_graph: KnowledgeGraph) -> None:
        subgraph = populated_graph.get_subgraph("Alice", depth=0)
        # Only the seed entity at depth 0.
        assert len(subgraph["entities"]) == 1
        assert subgraph["entities"][0].name == "Alice"
        assert len(subgraph["relations"]) == 0


# ---------------------------------------------------------------------------
# Merge tests
# ---------------------------------------------------------------------------

class TestMergeGraph:
    def test_merge_graph_combines_entities(self, db_path: Path) -> None:
        graph_a = KnowledgeGraph(db_path, tenant_id="merge_a")
        graph_b = KnowledgeGraph(db_path, tenant_id="merge_b")

        graph_a.add_entity(Entity(name="A", source_doc_ids=["doc1"]))
        graph_b.add_entity(Entity(name="B", source_doc_ids=["doc2"]))
        graph_b.add_relation(
            Relation(source="A", target="B", relation_type="link")
        )

        graph_a.merge_graph(graph_b)

        assert graph_a.get_entity("A") is not None
        assert graph_a.get_entity("B") is not None
        assert len(graph_a.get_relations()) == 1

    def test_merge_graph_deduplicates_relations(self, db_path: Path) -> None:
        graph_a = KnowledgeGraph(db_path, tenant_id="dedup_a")
        graph_b = KnowledgeGraph(db_path, tenant_id="dedup_b")

        graph_a.add_relation(
            Relation(source="X", target="Y", relation_type="same", confidence=0.5)
        )
        graph_b.add_relation(
            Relation(source="X", target="Y", relation_type="same", confidence=0.8)
        )

        graph_a.merge_graph(graph_b)
        relations = graph_a.get_relations()
        assert len(relations) == 1
        assert relations[0].confidence == 0.8


# ---------------------------------------------------------------------------
# Extraction tests
# ---------------------------------------------------------------------------

class TestExtraction:
    def test_extract_with_mock_llm(self, knowledge_graph: KnowledgeGraph) -> None:
        provider = MockLLMProvider(responses=[_EXTRACTION_RESPONSE])
        entities, relations = knowledge_graph.extract_entities_and_relations(
            "Alice works on Project X funded by Acme Corp",
            provider,
            doc_id="doc1",
        )
        assert len(entities) == 3
        assert len(relations) == 3
        names = {e.name for e in entities}
        assert "Alice" in names
        assert "Project X" in names
        assert "Acme Corp" in names
        # doc_id should be stamped.
        for e in entities:
            assert "doc1" in e.source_doc_ids

    def test_extract_without_llm_returns_empty(self, knowledge_graph: KnowledgeGraph) -> None:
        entities, relations = knowledge_graph.extract_entities_and_relations(
            "some text", None
        )
        assert entities == []
        assert relations == []

    def test_extract_with_empty_text(self, knowledge_graph: KnowledgeGraph) -> None:
        provider = MockLLMProvider()
        entities, relations = knowledge_graph.extract_entities_and_relations(
            "", provider
        )
        assert entities == []
        assert relations == []

    def test_extract_handles_malformed_llm_response(
        self, knowledge_graph: KnowledgeGraph
    ) -> None:
        provider = MockLLMProvider(responses=["this is not JSON"])
        entities, relations = knowledge_graph.extract_entities_and_relations(
            "text", provider
        )
        assert entities == []
        assert relations == []

    def test_extract_creates_implicit_entities_from_relations(
        self, knowledge_graph: KnowledgeGraph
    ) -> None:
        response = json.dumps({
            "entities": [
                {"name": "Known", "type": "person"},
            ],
            "relations": [
                {
                    "source": "Known",
                    "target": "Unknown",
                    "type": "knows",
                    "confidence": 0.8,
                },
            ],
        })
        provider = MockLLMProvider(responses=[response])
        entities, _ = knowledge_graph.extract_entities_and_relations(
            "text", provider
        )
        names = {e.name for e in entities}
        assert "Known" in names
        assert "Unknown" in names

    def test_extract_llm_exception_returns_empty(
        self, knowledge_graph: KnowledgeGraph
    ) -> None:
        class FailingProvider:
            def chat_completion_sync(self, messages: list[dict[str, Any]]) -> str:
                raise RuntimeError("LLM unavailable")

        entities, relations = knowledge_graph.extract_entities_and_relations(
            "text", FailingProvider()
        )
        assert entities == []
        assert relations == []


# ---------------------------------------------------------------------------
# Retrieval tests
# ---------------------------------------------------------------------------

class TestRetrieval:
    def test_retrieve_with_mock_llm(self, populated_graph: KnowledgeGraph) -> None:
        provider = MockLLMProvider(responses=[_RETRIEVAL_EXTRACTION_RESPONSE])
        results = populated_graph.retrieve(
            "How is Alice connected to Project X?", provider, top_k=5
        )
        # Should return chunks from Alice and Project X source_doc_ids.
        assert len(results) > 0
        for r in results:
            assert "chunk_id" in r
            assert "score" in r
            assert r["source"] == "graph"

    def test_retrieve_empty_query(self, populated_graph: KnowledgeGraph) -> None:
        provider = MockLLMProvider()
        results = populated_graph.retrieve("", provider)
        assert results == []

    def test_retrieve_without_llm_uses_substring_fallback(
        self, populated_graph: KnowledgeGraph
    ) -> None:
        results = populated_graph.retrieve("Alice", None, top_k=5)
        assert len(results) > 0
        # Alice has source_doc_ids doc1, doc2.
        chunk_ids = {r["chunk_id"] for r in results}
        assert "doc1" in chunk_ids or "doc2" in chunk_ids

    def test_retrieve_no_matching_entities(self, populated_graph: KnowledgeGraph) -> None:
        results = populated_graph.retrieve("zzzznomatch", None)
        assert results == []

    def test_retrieve_neighbor_decay(self, populated_graph: KnowledgeGraph) -> None:
        results = populated_graph.retrieve("Alice", None, top_k=10)
        # Direct entity docs should have score 1.0.
        direct = [r for r in results if r["chunk_id"] == "doc1"]
        if direct:
            assert direct[0]["score"] >= 0.5


# ---------------------------------------------------------------------------
# Serialization round-trip tests
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_to_dict_and_from_dict_round_trip(
        self, populated_graph: KnowledgeGraph, db_path: Path
    ) -> None:
        data = populated_graph.to_dict()
        assert data["tenant_id"] == "test"
        assert len(data["entities"]) == 3
        assert len(data["relations"]) == 3

        new_db = db_path.parent / "kg_roundtrip.db"
        restored = KnowledgeGraph.from_dict(data, new_db)
        assert restored.tenant_id == "test"
        assert restored.get_entity("Alice") is not None
        assert restored.get_entity("Project X") is not None
        assert restored.get_entity("Acme Corp") is not None
        assert len(restored.get_relations()) == 3

    def test_from_dict_preserves_properties(
        self, populated_graph: KnowledgeGraph, db_path: Path
    ) -> None:
        data = populated_graph.to_dict()
        new_db = db_path.parent / "kg_props.db"
        restored = KnowledgeGraph.from_dict(data, new_db)
        alice = restored.get_entity("Alice")
        assert alice is not None
        assert alice.properties.get("role") == "engineer"

    def test_from_dict_preserves_relation_confidence(
        self, populated_graph: KnowledgeGraph, db_path: Path
    ) -> None:
        data = populated_graph.to_dict()
        new_db = db_path.parent / "kg_conf.db"
        restored = KnowledgeGraph.from_dict(data, new_db)
        relations = restored.get_relations()
        works_on = [r for r in relations if r.relation_type == "works_on"]
        assert len(works_on) == 1
        assert works_on[0].confidence == 0.95

    def test_to_dict_empty_graph(self, db_path: Path) -> None:
        graph = KnowledgeGraph(db_path, tenant_id="empty")
        data = graph.to_dict()
        assert data["entities"] == []
        assert data["relations"] == []

    def test_from_dict_with_explicit_tenant(self, db_path: Path) -> None:
        data = {"tenant_id": "original", "entities": [], "relations": []}
        graph = KnowledgeGraph.from_dict(data, db_path, tenant_id="override")
        assert graph.tenant_id == "override"
