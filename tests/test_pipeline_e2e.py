# mypy: ignore-errors
"""End-to-end tests for the processing pipeline."""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from doctoragent.api.schemas import ClassificationResult, FileEvent, SearchQuery
from doctoragent.config import AegisConfig
from doctoragent.connections.models import Connection, PlatformType
from doctoragent.execution.vault import VaultManager
from doctoragent.model.classifier import Classifier
from doctoragent.model.provider import ModelProvider
from doctoragent.orchestration.agent import AegisAgent
from doctoragent.orchestration.pipeline import ProcessingPipeline
from doctoragent.orchestration.state_machine import TaskState
from doctoragent.orchestration.task_store import TaskStore
from doctoragent.security.audit_log import AuditLogger
from doctoragent.security.keytree import derive_vault_key
from doctoragent.security.policy import SecurityPolicyError


class FakeProvider(ModelProvider):
    """Fake model provider for testing."""

    def __init__(self, response: str) -> None:
        self.response = response

    async def chat_completion(self, messages: list[dict[str, object]]) -> str:
        return self.response

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        pass


@pytest.fixture
def local_connection() -> Connection:
    """Trusted local connection for tests."""
    return Connection(
        name="Local Test",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        is_local=True,
    )


@pytest.fixture
def classifier(local_connection: Connection) -> Classifier:
    """Classifier backed by fake provider."""
    response = json.dumps(
        {
            "sensitivity": "medium",
            "category": "work",
            "tags": ["report"],
            "summary": "A work report",
            "disguise_name": "team_building_2023",
            "disguise_extension": "log",
        }
    )
    return Classifier(FakeProvider(response), local_connection)


@pytest.fixture
def config(tmp_path: Path) -> AegisConfig:
    """Test configuration with isolated paths."""
    cfg = AegisConfig()
    cfg.paths.inbox = tmp_path / "Inbox"
    cfg.paths.vault = tmp_path / "Vault"
    cfg.paths.index = tmp_path / "Index"
    return cfg


@pytest.fixture
def vault_key() -> bytes:
    """Deterministic vault key for tests."""
    return derive_vault_key(b"0" * 32)


async def test_pipeline_encrypts_and_deletes_source(
    tmp_path: Path,
    config: AegisConfig,
    classifier: Classifier,
    vault_key: bytes,
) -> None:
    """Full pipeline moves file from Inbox to Vault and deletes source."""
    config.paths.inbox.mkdir(parents=True, exist_ok=True)
    source = config.paths.inbox / "secret.txt"
    source.write_text("top secret report")

    task_store = TaskStore(config.paths.index / "tasks.db")
    pipeline = ProcessingPipeline(config, classifier, task_store, vault_key)

    event = FileEvent(event_id=uuid4(), source_path=source)
    status = await pipeline.process(event)

    record = task_store.get(event.event_id)
    message = record.get("message") if record else "no record"
    assert status.state == TaskState.COMPLETED.name, message
    assert not source.exists()
    assert any(config.paths.vault.rglob("*.log"))

    record = task_store.get(event.event_id)
    assert record is not None
    assert record["vault_path"] is not None


async def test_pipeline_uses_injected_vault_manager(
    tmp_path: Path,
    config: AegisConfig,
    classifier: Classifier,
    vault_key: bytes,
) -> None:
    """ProcessingPipeline accepts an injected VaultManager instance."""
    config.paths.inbox.mkdir(parents=True, exist_ok=True)
    source = config.paths.inbox / "secret.txt"
    source.write_text("injected vault manager")

    task_store = TaskStore(config.paths.index / "tasks.db")
    vault_manager = VaultManager(config.paths.vault, vault_key)
    pipeline = ProcessingPipeline(
        config, classifier, task_store, vault_key, vault_manager=vault_manager
    )

    event = FileEvent(event_id=uuid4(), source_path=source)
    status = await pipeline.process(event)

    record = task_store.get(event.event_id)
    message = record.get("message") if record else "no record"
    assert status.state == TaskState.COMPLETED.name, message
    assert not source.exists()
    assert any(config.paths.vault.rglob("*.log"))


async def test_pipeline_quarantines_on_security_policy_error(
    tmp_path: Path,
    config: AegisConfig,
    classifier: Classifier,
    vault_key: bytes,
) -> None:
    """Security policy violations transition the task to QUARANTINED."""
    config.paths.inbox.mkdir(parents=True, exist_ok=True)
    source = config.paths.inbox / "secret.txt"
    source.write_text("sensitive data")

    task_store = TaskStore(config.paths.index / "tasks.db")
    pipeline = ProcessingPipeline(config, classifier, task_store, vault_key)

    async def failing_classify(_source_path: Path) -> ClassificationResult:
        raise SecurityPolicyError("untrusted connection")

    pipeline._classify = failing_classify  # type: ignore[method-assign]

    event = FileEvent(event_id=uuid4(), source_path=source)
    status = await pipeline.process(event)

    assert status.state == TaskState.QUARANTINED.name
    assert "untrusted connection" in status.message


async def test_pipeline_fails_on_generic_exception(
    tmp_path: Path,
    config: AegisConfig,
    classifier: Classifier,
    vault_key: bytes,
) -> None:
    """Unexpected exceptions transition the task to FAILED."""
    config.paths.inbox.mkdir(parents=True, exist_ok=True)
    source = config.paths.inbox / "secret.txt"
    source.write_text("sensitive data")

    task_store = TaskStore(config.paths.index / "tasks.db")
    pipeline = ProcessingPipeline(config, classifier, task_store, vault_key)

    async def failing_classify(_source_path: Path) -> ClassificationResult:
        raise ValueError("classifier exploded")

    pipeline._classify = failing_classify  # type: ignore[method-assign]

    event = FileEvent(event_id=uuid4(), source_path=source)
    status = await pipeline.process(event)

    assert status.state == TaskState.FAILED.name
    assert "classifier exploded" in status.message


def test_secure_delete_missing_file_is_no_op(
    config: AegisConfig,
    classifier: Classifier,
    vault_key: bytes,
) -> None:
    """_secure_delete silently returns when the source file no longer exists."""
    task_store = TaskStore(config.paths.index / "tasks.db")
    pipeline = ProcessingPipeline(config, classifier, task_store, vault_key)

    missing = config.paths.inbox / "missing.txt"
    pipeline._secure_delete(missing)

    assert not missing.exists()


class _FixedMasterKeyProvider:
    """Deterministic master key provider for end-to-end tests."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def get_key(self) -> bytes:
        return self._key

    def exists(self) -> bool:
        return True


@pytest.mark.slow
@pytest.mark.skipif(sys.platform == "win32", reason="timing-sensitive on Windows")
async def test_e2e_file_drop_triggers_full_pipeline(
    tmp_path: Path,
    classifier: Classifier,
    vault_key: bytes,
) -> None:
    """Dropping a file into Inbox triggers the full pipeline automatically."""
    config = AegisConfig()
    config.paths.inbox = tmp_path / "Inbox"
    config.paths.vault = tmp_path / "Vault"
    config.paths.index = tmp_path / "Index"
    config.paths.connections = tmp_path / "Config" / "connections.json"
    config.paths.inbox.mkdir(parents=True, exist_ok=True)

    task_store = TaskStore(config.paths.index / "tasks.db")
    vault_manager = VaultManager(config.paths.vault, vault_key)
    agent = AegisAgent(
        config,
        classifier=classifier,
        task_store=task_store,
        master_key_provider=_FixedMasterKeyProvider(vault_key),  # type: ignore[arg-type]
        vault_manager=vault_manager,
    )

    captured_events: list[FileEvent] = []
    original_on_file_event = agent.on_file_event

    async def tracking_on_file_event(event: FileEvent) -> object:
        captured_events.append(event)
        return await original_on_file_event(event)

    agent.on_file_event = tracking_on_file_event  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    agent.start_monitoring(loop)
    try:
        source = config.paths.inbox / "secret.txt"
        source.write_text("top secret quarterly report")

        for _ in range(100):
            if captured_events:
                break
            await asyncio.sleep(0.05)

        assert len(captured_events) == 1
        task_id = captured_events[0].event_id

        for _ in range(100):
            status = agent.get_status(task_id)
            if status and status.state == TaskState.COMPLETED.name:
                break
            await asyncio.sleep(0.05)

        status = agent.get_status(task_id)
        assert status is not None
        assert status.state == TaskState.COMPLETED.name
        assert not source.exists()
        assert any(config.paths.vault.rglob("*.log"))

        results = await agent.search(SearchQuery(query="report"))
        assert len(results) >= 1
        assert results[0].category == "work"
    finally:
        agent.stop_monitoring()


async def test_pipeline_emits_audit_events(
    tmp_path: Path,
    config: AegisConfig,
    classifier: Classifier,
    vault_key: bytes,
) -> None:
    """The pipeline writes file_ingested, classified and encrypted audit events."""
    config.paths.inbox.mkdir(parents=True, exist_ok=True)
    config.paths.logs = tmp_path / "logs"
    source = config.paths.inbox / "secret.txt"
    source.write_text("audit test")

    task_store = TaskStore(config.paths.index / "tasks.db")
    audit = AuditLogger(config, hmac_key=b"k" * 32)
    pipeline = ProcessingPipeline(config, classifier, task_store, vault_key, audit_logger=audit)

    event = FileEvent(event_id=uuid4(), source_path=source)
    status = await pipeline.process(event)

    record = task_store.get(event.event_id)
    message = record.get("message") if record else "no record"
    assert status.state == TaskState.COMPLETED.name, message

    records = audit.query()
    types = [r["event_type"] for r in records]
    assert "file_ingested" in types
    assert "classified" in types
    assert "encrypted" in types


async def test_pipeline_quarantine_emits_policy_violation(
    tmp_path: Path,
    config: AegisConfig,
    classifier: Classifier,
    vault_key: bytes,
) -> None:
    """A SecurityPolicyError during processing is logged as policy_violation."""
    config.paths.inbox.mkdir(parents=True, exist_ok=True)
    config.paths.logs = tmp_path / "logs"
    source = config.paths.inbox / "secret.txt"
    source.write_text("sensitive data")

    task_store = TaskStore(config.paths.index / "tasks.db")
    audit = AuditLogger(config, hmac_key=b"k" * 32)
    pipeline = ProcessingPipeline(config, classifier, task_store, vault_key, audit_logger=audit)

    async def failing_classify(_source_path: Path) -> ClassificationResult:
        raise SecurityPolicyError("untrusted connection")

    pipeline._classify = failing_classify  # type: ignore[method-assign]

    event = FileEvent(event_id=uuid4(), source_path=source)
    status = await pipeline.process(event)

    assert status.state == TaskState.QUARANTINED.name
    records = audit.query(event_type="policy_violation")
    assert len(records) == 1
    assert "untrusted connection" in records[0]["details"]["error"]
