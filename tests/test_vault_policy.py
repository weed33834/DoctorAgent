# mypy: ignore-errors
"""Tests for Vault sensitive operation policy enforcement."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from doctoragent.api.schemas import ClassificationResult, FileEvent
from doctoragent.config import AegisConfig
from doctoragent.connections.models import Connection, PlatformType
from doctoragent.execution.vault import VaultManager
from doctoragent.model.classifier import Classifier
from doctoragent.model.provider import ModelProvider
from doctoragent.orchestration.pipeline import ProcessingPipeline
from doctoragent.orchestration.state_machine import TaskState
from doctoragent.orchestration.task_store import TaskStore
from doctoragent.security.audit_log import AuditLogger
from doctoragent.security.keytree import derive_vault_key
from doctoragent.security.policy import SecurityPolicyError, require_trusted_local_connection


class FakeProvider(ModelProvider):
    """Minimal model provider for policy tests."""

    def __init__(self, response: str = "") -> None:
        self.response = response

    async def chat_completion(self, messages: list[dict[str, object]]) -> str:
        return self.response

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        pass


@pytest.fixture
def vault_key() -> bytes:
    """Fixture for a deterministic test vault key."""
    master = b"0" * 32
    return derive_vault_key(master)


@pytest.fixture
def local_connection() -> Connection:
    """Trusted local connection."""
    return Connection(
        name="Local Ollama",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        is_local=True,
    )


@pytest.fixture
def cloud_connection() -> Connection:
    """Unauthorized cloud connection."""
    return Connection(
        name="Cloud OpenAI",
        platform_type=PlatformType.OPENAI,
        base_url="https://api.openai.com/v1",
        is_local=False,
    )


@pytest.fixture
def classification_response() -> str:
    """Valid classification JSON for the fake provider."""
    return json.dumps(
        {
            "sensitivity": "low",
            "category": "other",
            "tags": [],
            "summary": "",
            "disguise_name": "neutral",
            "disguise_extension": "log",
        }
    )


def test_require_trusted_local_rejects_cloud(cloud_connection: Connection) -> None:
    """The security helper rejects cloud connections."""
    with pytest.raises(SecurityPolicyError):
        require_trusted_local_connection(cloud_connection)


def test_require_trusted_local_accepts_local(local_connection: Connection) -> None:
    """The security helper accepts trusted local connections."""
    require_trusted_local_connection(local_connection)


def test_vault_encrypt_does_not_require_connection(
    tmp_path: Path,
    vault_key: bytes,
) -> None:
    """VaultManager is a pure crypto primitive and accepts raw inputs."""
    source = tmp_path / "secret.txt"
    source.write_bytes(b"secret")
    manager = VaultManager(tmp_path / "vault", vault_key)
    classification = ClassificationResult(
        sensitivity="low",
        category="other",
        disguise_name="neutral",
        disguise_extension="log",
    )

    result = manager.encrypt(source, classification, uuid4())
    assert result.vault_path.exists()


def test_vault_decrypt_does_not_require_connection(
    tmp_path: Path,
    vault_key: bytes,
) -> None:
    """VaultManager.decrypt accepts raw inputs without a Connection."""
    manager = VaultManager(tmp_path / "vault", vault_key)
    # Decryption of a non-existent file fails for IO reasons, not policy reasons.
    with pytest.raises(FileNotFoundError):
        manager.decrypt(tmp_path / "fake.vault", b"0" * 32, tmp_path / "out.txt")


async def test_pipeline_quarantines_cloud_connection(
    tmp_path: Path,
    vault_key: bytes,
    cloud_connection: Connection,
    classification_response: str,
) -> None:
    """Pipeline quarantines a file when the active connection is not trusted local."""
    config = AegisConfig()
    config.paths.inbox = tmp_path / "Inbox"
    config.paths.vault = tmp_path / "Vault"
    config.paths.index = tmp_path / "Index"
    config.paths.inbox.mkdir(parents=True, exist_ok=True)

    source = config.paths.inbox / "secret.txt"
    source.write_text("secret")

    classifier = Classifier(FakeProvider(classification_response), cloud_connection)
    task_store = TaskStore(config.paths.index / "tasks.db")
    pipeline = ProcessingPipeline(config, classifier, task_store, vault_key)

    event = FileEvent(event_id=uuid4(), source_path=source)
    status = await pipeline.process(event)

    assert status.state == TaskState.QUARANTINED.name
    assert source.exists()  # source must not be encrypted/removed


def test_vault_decrypt_logs_audit_event(
    tmp_path: Path,
    vault_key: bytes,
) -> None:
    """VaultManager.decrypt emits a decrypted audit event when an auditor is attached."""
    config = AegisConfig()
    config.paths.logs = tmp_path / "logs"
    audit = AuditLogger(config, hmac_key=b"k" * 32)

    source = tmp_path / "secret.txt"
    source.write_bytes(b"secret")
    manager = VaultManager(tmp_path / "vault", vault_key, audit_logger=audit)
    classification = ClassificationResult(
        sensitivity="low",
        category="other",
        disguise_name="neutral",
        disguise_extension="log",
    )

    encrypt_result = manager.encrypt(source, classification, uuid4())
    destination = tmp_path / "out.txt"
    manager.decrypt(encrypt_result.vault_path, encrypt_result.salt, destination)

    assert destination.read_bytes() == b"secret"
    records = audit.query(event_type="decrypted")
    assert len(records) == 1
    assert records[0]["details"]["vault_path"] == str(encrypt_result.vault_path)
