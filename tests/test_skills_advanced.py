# mypy: ignore-errors
"""Tests for the advanced skills system.

Covers:
* :class:`SemanticSkillMatcher` — TF-IDF + trigger + Jaccard confidence scoring.
* :class:`SkillComposer` / :class:`ComposedSkill` — skill chaining workflows.
* Seven new built-in skills: DocumentLifecycle, BatchOperation, SecurityAudit,
  SyncManagement, BackupRestore, Compliance, KnowledgeGraphQuery.
* :class:`SkillRegistry` (advanced) — semantic matching, best-match execution,
  and JSON save/load round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from doctoragent.model.skills import (
    Skill,
    SkillCategory,
    SkillDefinition,
    SkillResult,
)
from doctoragent.model.skills_advanced import (
    AuditScope,
    BackupAction,
    BatchOperationType,
    ComplianceCheckSkill,
    ComplianceFramework,
    ComposedSkill,
    DocumentLifecycleSkill,
    KnowledgeGraphQuerySkill,
    LifecycleOperation,
    SecurityAuditSkill,
    SemanticSkillMatcher,
    SkillComposer,
    SkillRegistry,
    SyncAction,
    SyncManagementSkill,
    BackupRestoreSkill,
    BatchOperationSkill,
    create_default_registry,
)


# ---------------------------------------------------------------------------
# Simple stub skill for composition / matcher tests
# ---------------------------------------------------------------------------

class StubSkill(Skill):
    """A configurable stub skill for deterministic testing."""

    def __init__(
        self,
        name: str,
        description: str = "",
        triggers: list[str] | None = None,
        examples: list[str] | None = None,
        category: SkillCategory = SkillCategory.MANAGEMENT,
        result_value: Any = "stub-result",
    ) -> None:
        self._name = name
        self._description = description or f"Stub skill {name}"
        self._triggers = triggers or []
        self._examples = examples or []
        self._category = category
        self._result_value = result_value

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name=self._name,
            description=self._description,
            category=self._category,
            triggers=self._triggers,
            examples=self._examples,
        )

    async def execute(
        self, query: str, context: dict[str, Any] | None = None
    ) -> SkillResult:
        return SkillResult(
            success=True,
            skill_name=self._name,
            result=self._result_value,
            steps_taken=[f"executed:{self._name}"],
            metadata={"query": query},
        )


class FailingStubSkill(Skill):
    """A stub skill that always returns a failed result."""

    def __init__(self, name: str = "failing_skill") -> None:
        self._name = name

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name=self._name,
            description="A skill that always fails",
            category=SkillCategory.MANAGEMENT,
            triggers=["fail"],
        )

    async def execute(
        self, query: str, context: dict[str, Any] | None = None
    ) -> SkillResult:
        return SkillResult(
            success=False,
            skill_name=self._name,
            error="Intentional failure for testing",
        )


class ContextSpySkill(Skill):
    """A skill that records the context it was called with."""

    def __init__(self, name: str = "context_spy") -> None:
        self._name = name
        self.last_context: dict[str, Any] | None = None

    @property
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name=self._name,
            description="Records the execution context",
            category=SkillCategory.ANALYSIS,
            triggers=["spy"],
        )

    async def execute(
        self, query: str, context: dict[str, Any] | None = None
    ) -> SkillResult:
        self.last_context = dict(context or {})
        prev = self.last_context.get("previous_result")
        return SkillResult(
            success=True,
            skill_name=self._name,
            result=f"saw:{prev}",
            steps_taken=[f"executed:{self._name}"],
        )


# ===========================================================================
# SemanticSkillMatcher
# ===========================================================================

class TestSemanticSkillMatcher:
    """Tests for :class:`SemanticSkillMatcher`."""

    @pytest.fixture
    def skills(self) -> list[Skill]:
        return [
            StubSkill(
                name="document_search",
                description="Search for documents in the vault",
                triggers=["search", "find", "look for"],
                examples=["Find my contracts", "Search for invoices"],
            ),
            StubSkill(
                name="document_analysis",
                description="Analyze and summarize documents",
                triggers=["analyze", "summarize", "summary"],
                examples=["Summarize the contract", "Analyze the report"],
            ),
            StubSkill(
                name="backup_restore",
                description="Create backups and restore documents",
                triggers=["backup", "restore", "recovery"],
                examples=["Create a backup", "Restore from yesterday"],
            ),
        ]

    @pytest.fixture
    def matcher(self, skills: list[Skill]) -> SemanticSkillMatcher:
        return SemanticSkillMatcher(skills)

    def test_match_returns_ranked_results(self, matcher: SemanticSkillMatcher) -> None:
        results = matcher.match("search for my documents")
        assert len(results) > 0
        # The document_search skill should rank highest.
        assert results[0][0].definition.name == "document_search"
        assert results[0][1] > 0.0

    def test_match_confidence_in_range(self, matcher: SemanticSkillMatcher) -> None:
        results = matcher.match("analyze the financial report")
        for _skill, confidence in results:
            assert 0.0 < confidence <= 1.0

    def test_match_no_results_for_unrelated_query(self, matcher: SemanticSkillMatcher) -> None:
        results = matcher.match("xyzqwerty nonsense")
        # Either no results or very low confidence.
        for _skill, confidence in results:
            assert confidence <= 0.2

    def test_match_top_k_limit(self, matcher: SemanticSkillMatcher) -> None:
        results = matcher.match("search analyze backup", top_k=2)
        assert len(results) <= 2

    def test_match_empty_query(self, matcher: SemanticSkillMatcher) -> None:
        assert matcher.match("") == []

    def test_match_trigger_substring_matching(self, matcher: SemanticSkillMatcher) -> None:
        """Trigger keywords appearing in the query boost the score."""
        results = matcher.match("please summarize my document")
        assert len(results) > 0
        top_name = results[0][0].definition.name
        assert top_name == "document_analysis"

    def test_match_fallback_substring(self) -> None:
        """When no semantic overlap, substring matching provides a small score."""
        skill = StubSkill(
            name="special_tool",
            description="A unique tool for special tasks",
            triggers=["nonexistent_trigger"],
        )
        matcher = SemanticSkillMatcher([skill])
        results = matcher.match("special tool for tasks")
        # Should match via fallback substring on the name/description.
        assert len(results) >= 1
        assert results[0][1] > 0.0

    def test_match_empty_skills_list(self) -> None:
        matcher = SemanticSkillMatcher([])
        assert matcher.match("anything") == []


# ===========================================================================
# SkillComposer & ComposedSkill
# ===========================================================================

class TestSkillComposer:
    """Tests for :class:`SkillComposer` and :class:`ComposedSkill`."""

    @pytest.fixture
    def composer(self) -> SkillComposer:
        return SkillComposer()

    @pytest.fixture
    def stub_skills(self) -> list[Skill]:
        return [
            StubSkill("step_one", result_value="result_one"),
            StubSkill("step_two", result_value="result_two"),
            StubSkill("step_three", result_value="result_three"),
        ]

    async def test_compose_creates_composed_skill(
        self, composer: SkillComposer, stub_skills: list[Skill]
    ) -> None:
        composed = composer.compose(
            stub_skills,
            name="my_workflow",
            description="A test workflow",
        )
        assert isinstance(composed, ComposedSkill)
        assert composed.definition.name == "my_workflow"
        assert composed.definition.description == "A test workflow"

    async def test_composed_skill_execute_chaining(
        self, composer: SkillComposer, stub_skills: list[Skill]
    ) -> None:
        """Each step's result is threaded forward as previous_result."""
        spy = ContextSpySkill(name="spy_step")
        skills = [stub_skills[0], spy]
        composed = composer.compose(skills, name="chain", description="chain test")
        result = await composed.execute("test query", context={"initial": "ctx"})
        assert result.success is True
        # The spy should have seen the first skill's result.
        assert spy.last_context is not None
        assert spy.last_context["previous_result"] == "result_one"
        # The composed result is the last step's result.
        assert result.result == "saw:result_one"

    async def test_composed_skill_stops_on_failure(
        self, composer: SkillComposer
    ) -> None:
        failing = FailingStubSkill(name="fail_step")
        good = StubSkill("good_step", result_value="should_not_reach")
        composed = composer.compose([failing, good], name="fail_chain", description="stops on failure")
        result = await composed.execute("test")
        assert result.success is False
        assert result.metadata.get("failed_at") == "fail_step"
        assert "should_not_reach" not in str(result.result)

    async def test_composed_skill_metadata(
        self, composer: SkillComposer, stub_skills: list[Skill]
    ) -> None:
        composed = composer.compose(stub_skills, name="meta", description="metadata test")
        result = await composed.execute("query")
        assert result.success is True
        assert "composed_of" in result.metadata
        assert result.metadata["composed_of"] == ["step_one", "step_two", "step_three"]

    async def test_composed_skill_definition_merges_triggers(
        self, composer: SkillComposer
    ) -> None:
        s1 = StubSkill("s1", triggers=["alpha", "beta"])
        s2 = StubSkill("s2", triggers=["beta", "gamma"])
        composed = composer.compose([s1, s2], name="merged", description="merge test")
        triggers = composed.definition.triggers
        assert "alpha" in triggers
        assert "beta" in triggers
        assert "gamma" in triggers
        # No duplicates.
        assert len(triggers) == len(set(triggers))

    async def test_create_workflow(self, composer: SkillComposer) -> None:
        """create_workflow builds a ComposedSkill from (skill, description) pairs."""
        s1 = StubSkill("first")
        s2 = StubSkill("second")
        composed = composer.create_workflow([
            (s1, "Extract data"),
            (s2, "Format output"),
        ])
        assert "first" in composed.definition.name
        assert "second" in composed.definition.name
        assert "Extract data" in composed.definition.description
        assert "Format output" in composed.definition.description

    async def test_composed_skill_empty_chain(self, composer: SkillComposer) -> None:
        composed = composer.compose([], name="empty", description="no steps")
        result = await composed.execute("query")
        assert result.success is True
        assert result.result is None


# ===========================================================================
# Built-in skill: DocumentLifecycle
# ===========================================================================

class TestDocumentLifecycleSkill:
    """Tests for :class:`DocumentLifecycleSkill`."""

    @pytest.fixture
    def skill(self) -> DocumentLifecycleSkill:
        return DocumentLifecycleSkill()

    async def test_create_document(self, skill: DocumentLifecycleSkill) -> None:
        result = await skill.execute("Create a new document titled Meeting Notes")
        assert result.success is True
        assert result.metadata["operation"] == LifecycleOperation.CREATE
        assert result.metadata["target"] == "Meeting Notes"
        assert result.metadata["simulated"] is True

    async def test_archive_document(self, skill: DocumentLifecycleSkill) -> None:
        result = await skill.execute("Archive the old invoices")
        assert result.success is True
        assert result.metadata["operation"] == LifecycleOperation.ARCHIVE

    async def test_delete_document(self, skill: DocumentLifecycleSkill) -> None:
        result = await skill.execute("Delete the draft report")
        assert result.success is True
        assert result.metadata["operation"] == LifecycleOperation.DELETE

    async def test_restore_document(self, skill: DocumentLifecycleSkill) -> None:
        result = await skill.execute("Restore my contract")
        assert result.success is True
        assert result.metadata["operation"] == LifecycleOperation.RESTORE

    async def test_update_document(self, skill: DocumentLifecycleSkill) -> None:
        result = await skill.execute("Update the project plan")
        assert result.success is True
        assert result.metadata["operation"] == LifecycleOperation.UPDATE

    async def test_no_operation_detected(self, skill: DocumentLifecycleSkill) -> None:
        result = await skill.execute("Show me the weather")
        assert result.success is False
        assert "Could not determine" in (result.error or "")

    async def test_target_extraction_from_quotes(self, skill: DocumentLifecycleSkill) -> None:
        result = await skill.execute('Create a document named "Secret File"')
        assert result.success is True
        assert result.metadata["target"] == "Secret File"

    async def test_with_vault_manager(self) -> None:
        """When a vault manager is attached, the operation is delegated."""
        class MockVault:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def create(self, target: str | None) -> str:
                self.calls.append(f"create:{target}")
                return "created"

        vault = MockVault()
        skill = DocumentLifecycleSkill(vault_manager=vault)
        result = await skill.execute("Create a document titled Test")
        assert result.success is True
        assert result.metadata.get("simulated") is None
        assert "create:Test" in vault.calls


# ===========================================================================
# Built-in skill: BatchOperation
# ===========================================================================

class TestBatchOperationSkill:
    """Tests for :class:`BatchOperationSkill`."""

    @pytest.fixture
    def skill(self) -> BatchOperationSkill:
        return BatchOperationSkill()

    async def test_batch_classify(self, skill: BatchOperationSkill) -> None:
        result = await skill.execute("Batch classify all files in the invoices folder")
        assert result.success is True
        assert result.metadata["operation"] == BatchOperationType.CLASSIFY
        assert result.metadata["simulated"] is True

    async def test_bulk_encrypt(self, skill: BatchOperationSkill) -> None:
        result = await skill.execute("Bulk encrypt all PDFs")
        assert result.success is True
        assert result.metadata["operation"] == BatchOperationType.ENCRYPT

    async def test_batch_export(self, skill: BatchOperationSkill) -> None:
        result = await skill.execute("Export all files tagged as contracts")
        assert result.success is True
        assert result.metadata["operation"] == BatchOperationType.EXPORT

    async def test_batch_delete(self, skill: BatchOperationSkill) -> None:
        result = await skill.execute("Delete all archived files older than 2023")
        assert result.success is True
        assert result.metadata["operation"] == BatchOperationType.DELETE

    async def test_no_operation_type(self, skill: BatchOperationSkill) -> None:
        result = await skill.execute("Do something with files")
        assert result.success is False

    async def test_scope_all_files(self, skill: BatchOperationSkill) -> None:
        result = await skill.execute("Batch classify all files")
        assert result.success is True
        assert result.metadata["scope"] == "all"

    async def test_with_classifier(self) -> None:
        class MockClassifier:
            def batch_classify(self, scope: str | None) -> dict:
                return {"classified": 10}

        skill = BatchOperationSkill(classifier=MockClassifier())
        result = await skill.execute("Batch classify all files")
        assert result.success is True
        assert result.metadata.get("simulated") is None


# ===========================================================================
# Built-in skill: SecurityAudit
# ===========================================================================

class TestSecurityAuditSkill:
    """Tests for :class:`SecurityAuditSkill`."""

    @pytest.fixture
    def skill(self) -> SecurityAuditSkill:
        return SecurityAuditSkill()

    async def test_security_scan(self, skill: SecurityAuditSkill) -> None:
        result = await skill.execute("Run a security scan on the vault")
        assert result.success is True
        assert result.metadata["scope"] == AuditScope.SECURITY
        assert result.metadata["simulated"] is True

    async def test_vulnerability_audit(self, skill: SecurityAuditSkill) -> None:
        result = await skill.execute("Check for vulnerability in encrypted files")
        assert result.success is True
        assert result.metadata["scope"] == AuditScope.VULNERABILITY

    async def test_permission_audit(self, skill: SecurityAuditSkill) -> None:
        result = await skill.execute("Audit permissions for all documents")
        assert result.success is True
        assert result.metadata["scope"] == AuditScope.PERMISSIONS

    async def test_simulated_findings_have_recommendations(self, skill: SecurityAuditSkill) -> None:
        result = await skill.execute("Run a security scan")
        assert result.success is True
        assert isinstance(result.result, dict)
        assert "recommendations" in result.result
        assert len(result.result["recommendations"]) > 0

    async def test_with_scanner(self) -> None:
        class MockScanner:
            def scan(self) -> dict:
                return {"issues": 2, "severity": "low"}

        skill = SecurityAuditSkill(security_scanner=MockScanner())
        result = await skill.execute("Run a security scan")
        assert result.success is True
        assert result.metadata.get("simulated") is None


# ===========================================================================
# Built-in skill: SyncManagement
# ===========================================================================

class TestSyncManagementSkill:
    """Tests for :class:`SyncManagementSkill`."""

    @pytest.fixture
    def skill(self) -> SyncManagementSkill:
        return SyncManagementSkill()

    async def test_sync_action(self, skill: SyncManagementSkill) -> None:
        result = await skill.execute("Sync my vault now")
        assert result.success is True
        assert result.metadata["action"] == SyncAction.SYNC
        assert result.metadata["simulated"] is True

    async def test_resolve_conflict(self, skill: SyncManagementSkill) -> None:
        result = await skill.execute("Resolve sync conflicts")
        assert result.success is True
        assert result.metadata["action"] == SyncAction.RESOLVE_CONFLICT

    async def test_sync_status(self, skill: SyncManagementSkill) -> None:
        result = await skill.execute("Show sync status")
        assert result.success is True
        assert result.metadata["action"] == SyncAction.STATUS

    async def test_merge_action(self, skill: SyncManagementSkill) -> None:
        result = await skill.execute("Merge changes from the remote")
        assert result.success is True
        assert result.metadata["action"] == SyncAction.MERGE

    async def test_with_sync_engine(self) -> None:
        class MockSync:
            def sync(self) -> str:
                return "synced"

        skill = SyncManagementSkill(sync_engine=MockSync())
        result = await skill.execute("Sync now")
        assert result.success is True
        assert result.metadata.get("simulated") is None


# ===========================================================================
# Built-in skill: BackupRestore
# ===========================================================================

class TestBackupRestoreSkill:
    """Tests for :class:`BackupRestoreSkill`."""

    @pytest.fixture
    def skill(self) -> BackupRestoreSkill:
        return BackupRestoreSkill()

    async def test_backup_action(self, skill: BackupRestoreSkill) -> None:
        result = await skill.execute("Create a full backup of the vault")
        assert result.success is True
        assert result.metadata["action"] == BackupAction.BACKUP
        assert result.metadata["simulated"] is True

    async def test_verify_action(self, skill: BackupRestoreSkill) -> None:
        result = await skill.execute("Verify the latest backup")
        assert result.success is True
        assert result.metadata["action"] == BackupAction.VERIFY

    async def test_restore_action(self, skill: BackupRestoreSkill) -> None:
        result = await skill.execute("Restore documents from yesterday's backup")
        assert result.success is True
        assert result.metadata["action"] == BackupAction.RESTORE

    async def test_with_backup_manager(self) -> None:
        class MockBackup:
            def backup(self) -> str:
                return "snapshot-001"

        skill = BackupRestoreSkill(backup_manager=MockBackup())
        result = await skill.execute("Create a backup")
        assert result.success is True
        assert result.metadata.get("simulated") is None


# ===========================================================================
# Built-in skill: ComplianceCheck
# ===========================================================================

class TestComplianceCheckSkill:
    """Tests for :class:`ComplianceCheckSkill`."""

    @pytest.fixture
    def skill(self) -> ComplianceCheckSkill:
        return ComplianceCheckSkill()

    async def test_gdpr_compliance(self, skill: ComplianceCheckSkill) -> None:
        result = await skill.execute("Check GDPR compliance for the vault")
        assert result.success is True
        assert result.metadata["framework"] == ComplianceFramework.GDPR
        assert result.metadata["simulated"] is True

    async def test_ccpa_compliance(self, skill: ComplianceCheckSkill) -> None:
        result = await skill.execute("Is my vault CCPA compliant?")
        assert result.success is True
        assert result.metadata["framework"] == ComplianceFramework.CCPA

    async def test_retention_review(self, skill: ComplianceCheckSkill) -> None:
        result = await skill.execute("Review retention policies for financial documents")
        assert result.success is True
        assert result.metadata["framework"] == ComplianceFramework.RETENTION
        assert result.metadata["request_type"] == "retention_review"

    async def test_data_subject_access_request(self, skill: ComplianceCheckSkill) -> None:
        result = await skill.execute("Process a data subject access request")
        assert result.success is True
        assert result.metadata["request_type"] == "data_subject_access"

    async def test_data_subject_erasure_request(self, skill: ComplianceCheckSkill) -> None:
        result = await skill.execute("Process a data subject erasure request")
        assert result.success is True
        assert result.metadata["request_type"] == "data_subject_erasure"

    async def test_simulated_report_has_recommendations(self, skill: ComplianceCheckSkill) -> None:
        result = await skill.execute("Check GDPR compliance")
        assert result.success is True
        assert isinstance(result.result, dict)
        assert "recommendations" in result.result
        assert len(result.result["recommendations"]) > 0

    async def test_with_compliance_engine(self) -> None:
        class MockEngine:
            def compliance_check(self, framework: Any) -> dict:
                return {"compliant": True}

        skill = ComplianceCheckSkill(compliance_engine=MockEngine())
        result = await skill.execute("Check GDPR compliance")
        assert result.success is True
        assert result.metadata.get("simulated") is None


# ===========================================================================
# Built-in skill: KnowledgeGraphQuery
# ===========================================================================

class TestKnowledgeGraphQuerySkill:
    """Tests for :class:`KnowledgeGraphQuerySkill`."""

    @pytest.fixture
    def skill(self) -> KnowledgeGraphQuerySkill:
        return KnowledgeGraphQuerySkill()

    async def test_simulated_no_graph(self, skill: KnowledgeGraphQuerySkill) -> None:
        result = await skill.execute("How is Alice related to Project X?")
        assert result.success is True
        assert result.metadata["simulated"] is True

    async def test_with_mock_graph(self) -> None:
        class MockEntity:
            def __init__(self, name: str) -> None:
                self._name = name

            def to_dict(self) -> dict:
                return {"name": self._name, "type": "person"}

        class MockGraph:
            def get_entity(self, name: str) -> Any:
                if name in ("Alice", "Project X"):
                    return MockEntity(name)
                return None

            def get_relations(self, name: str) -> list:
                return []

            def get_subgraph(self, seed: str, depth: int = 2) -> dict:
                return {
                    "seed": seed,
                    "entities": [MockEntity(seed)],
                    "relations": [],
                }

        skill = KnowledgeGraphQuerySkill(knowledge_graph=MockGraph())
        result = await skill.execute("How is Alice related to Project X?")
        assert result.success is True
        assert "entity" in result.result
        assert result.result["entity"]["name"] in ("Alice", "Project X")

    async def test_entity_extraction_from_query(self, skill: KnowledgeGraphQuerySkill) -> None:
        """The skill heuristically extracts the seed entity from the query."""
        result = await skill.execute("How is Alice related to Project X?")
        assert result.success is True

    async def test_no_entity_match_falls_back_to_retrieve(self) -> None:
        class MockGraph:
            def retrieve(self, query: str, llm: Any = None, top_k: int = 5) -> list:
                return [{"text": "graph chunk"}]

        skill = KnowledgeGraphQuerySkill(knowledge_graph=MockGraph())
        result = await skill.execute("Find connections between unknown entities")
        assert result.success is True
        assert "retrieved_chunks" in result.result


# ===========================================================================
# SkillRegistry (advanced)
# ===========================================================================

class TestSkillRegistry:
    """Tests for the advanced :class:`SkillRegistry`."""

    @pytest.fixture
    def registry(self) -> SkillRegistry:
        return SkillRegistry()

    @pytest.fixture
    def populated_registry(self) -> SkillRegistry:
        registry = SkillRegistry()
        registry.register(DocumentLifecycleSkill())
        registry.register(BatchOperationSkill())
        registry.register(SecurityAuditSkill())
        registry.register(ComplianceCheckSkill())
        return registry

    # -- registration & listing -------------------------------------------

    def test_register_and_get(self, registry: SkillRegistry) -> None:
        skill = StubSkill("test_skill")
        registry.register(skill)
        assert registry.get_skill("test_skill") is skill

    def test_unregister(self, registry: SkillRegistry) -> None:
        skill = StubSkill("to_remove")
        registry.register(skill)
        removed = registry.unregister("to_remove")
        assert removed is skill
        assert registry.get_skill("to_remove") is None
        assert registry.unregister("nonexistent") is None

    def test_list_skills(self, registry: SkillRegistry) -> None:
        registry.register(StubSkill("a"))
        registry.register(StubSkill("b"))
        defs = registry.list_skills()
        names = [d.name for d in defs]
        assert "a" in names
        assert "b" in names

    # -- semantic matching -------------------------------------------------

    def test_match_skills(self, populated_registry: SkillRegistry) -> None:
        matches = populated_registry.match_skills("Create a new document")
        assert len(matches) > 0
        assert matches[0][0].definition.name == "document_lifecycle"
        assert matches[0][1] > 0.0

    def test_match_skills_top_k(self, populated_registry: SkillRegistry) -> None:
        matches = populated_registry.match_skills("security audit backup compliance", top_k=2)
        assert len(matches) <= 2

    def test_match_skills_no_match(self, registry: SkillRegistry) -> None:
        registry.register(StubSkill("niche", triggers=["very_specific_keyword"]))
        matches = registry.match_skills("completely unrelated query xyzqwerty")
        # Should return empty or very low confidence matches only.
        for _skill, confidence in matches:
            assert confidence <= 0.2

    # -- best match execution ---------------------------------------------

    async def test_execute_best_match(self, populated_registry: SkillRegistry) -> None:
        result = await populated_registry.execute_best_match("Create a document titled Report")
        assert result.success is True
        assert result.metadata.get("matched_skill") == "document_lifecycle"
        assert "match_confidence" in result.metadata

    async def test_execute_best_match_no_match(self, registry: SkillRegistry) -> None:
        result = await registry.execute_best_match("totally unknown query xyz")
        assert result.success is False
        assert "No matching skill" in (result.error or "")

    async def test_execute_best_match_includes_context(
        self, populated_registry: SkillRegistry
    ) -> None:
        ctx = {"user_id": "test_user", "session": "abc"}
        result = await populated_registry.execute_best_match(
            "Run a security scan", context=ctx
        )
        assert result.success is True
        assert result.metadata.get("matched_skill") == "security_audit"

    async def test_execute_best_match_skill_raises(self) -> None:
        """When a skill raises, the registry returns a failed SkillResult."""

        class RaisingSkill(Skill):
            @property
            def definition(self) -> SkillDefinition:
                return SkillDefinition(
                    name="raiser",
                    description="Always raises",
                    category=SkillCategory.MANAGEMENT,
                    triggers=["raise"],
                )

            async def execute(self, query: str, context: dict | None = None) -> SkillResult:
                raise RuntimeError("boom")

        registry = SkillRegistry()
        registry.register(RaisingSkill())
        result = await registry.execute_best_match("raise an error")
        assert result.success is False
        assert "boom" in (result.error or "")

    # -- persistence (save / load) ----------------------------------------

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        registry = SkillRegistry()
        registry.register(DocumentLifecycleSkill())
        registry.register(SecurityAuditSkill())
        registry.register(ComplianceCheckSkill())

        save_path = tmp_path / "skills.json"
        registry.save(save_path)
        assert save_path.exists()

        loaded_payload = json.loads(save_path.read_text(encoding="utf-8"))
        assert loaded_payload["version"] == 1
        skill_names = [s["name"] for s in loaded_payload["skills"]]
        assert "document_lifecycle" in skill_names
        assert "security_audit" in skill_names
        assert "compliance_check" in skill_names

        new_registry = SkillRegistry()
        new_registry.load(save_path)
        loaded_names = [d.name for d in new_registry.list_skills()]
        assert "document_lifecycle" in loaded_names
        assert "security_audit" in loaded_names

    def test_load_preserves_existing_concrete_skills(self, tmp_path: Path) -> None:
        """Skills already registered are not clobbered by loaded stubs."""
        registry = SkillRegistry()
        concrete = DocumentLifecycleSkill()
        registry.register(concrete)

        save_path = tmp_path / "skills.json"
        registry.save(save_path)

        new_registry = SkillRegistry()
        new_registry.register(DocumentLifecycleSkill())
        new_registry.load(save_path)
        # The concrete skill should still be the one registered.
        skill = new_registry.get_skill("document_lifecycle")
        assert skill is not None
        assert isinstance(skill, DocumentLifecycleSkill)

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        registry = SkillRegistry()
        registry.load(tmp_path / "does_not_exist.json")
        # Should not raise; registry stays empty.
        assert len(registry.list_skills()) == 0

    def test_load_malformed_file(self, tmp_path: Path) -> None:
        save_path = tmp_path / "bad.json"
        save_path.write_text("not valid json {{{", encoding="utf-8")
        registry = SkillRegistry()
        registry.load(save_path)
        assert len(registry.list_skills()) == 0

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        registry = SkillRegistry()
        registry.register(StubSkill("test"))
        save_path = tmp_path / "subdir" / "nested" / "skills.json"
        registry.save(save_path)
        assert save_path.exists()

    async def test_loaded_stub_skill_execute_fails(self, tmp_path: Path) -> None:
        """A persisted stub skill reports that it has no implementation."""
        registry = SkillRegistry()
        registry.register(DocumentLifecycleSkill())
        save_path = tmp_path / "skills.json"
        registry.save(save_path)

        new_registry = SkillRegistry()
        new_registry.load(save_path)
        skill = new_registry.get_skill("document_lifecycle")
        assert skill is not None
        result = await skill.execute("Create a document")
        assert result.success is False
        assert "no bound implementation" in (result.error or "")


# ===========================================================================
# create_default_registry
# ===========================================================================

class TestCreateDefaultRegistry:
    """Tests for the :func:`create_default_registry` factory."""

    def test_default_registry_has_all_skills(self) -> None:
        registry = create_default_registry()
        defs = registry.list_skills()
        names = {d.name for d in defs}
        expected = {
            "document_lifecycle",
            "batch_operation",
            "security_audit",
            "sync_management",
            "backup_restore",
            "compliance_check",
            "knowledge_graph_query",
        }
        assert expected == names

    async def test_default_registry_executes_lifecycle(self) -> None:
        registry = create_default_registry()
        result = await registry.execute_best_match("Create a document titled Test")
        assert result.success is True

    async def test_default_registry_executes_backup(self) -> None:
        registry = create_default_registry()
        result = await registry.execute_best_match("Create a backup")
        assert result.success is True

    async def test_default_registry_executes_compliance(self) -> None:
        registry = create_default_registry()
        result = await registry.execute_best_match("Check GDPR compliance")
        assert result.success is True

    async def test_default_registry_executes_sync(self) -> None:
        registry = create_default_registry()
        result = await registry.execute_best_match("Sync my vault now")
        assert result.success is True

    async def test_default_registry_executes_security(self) -> None:
        registry = create_default_registry()
        result = await registry.execute_best_match("Run a security scan")
        assert result.success is True

    async def test_default_registry_executes_batch(self) -> None:
        registry = create_default_registry()
        result = await registry.execute_best_match("Batch classify all files")
        assert result.success is True

    async def test_default_registry_executes_graph_query(self) -> None:
        registry = create_default_registry()
        result = await registry.execute_best_match("How is Alice related to Project X?")
        assert result.success is True
