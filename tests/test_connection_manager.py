# mypy: ignore-errors
"""Tests for platform connection manager."""

from pathlib import Path
from uuid import uuid4

import pytest

from doctoragent.connections.manager import ConnectionManager
from doctoragent.connections.models import Connection, PlatformType


@pytest.fixture
def manager(tmp_path: Path) -> ConnectionManager:
    """Fixture with isolated connection storage."""
    return ConnectionManager(tmp_path / "connections.json")


def test_default_connection_seeded(manager: ConnectionManager) -> None:
    """Manager seeds a default local Ollama connection."""
    conns = manager.list_all()
    assert len(conns) == 1
    assert conns[0].platform_type == PlatformType.OLLAMA
    assert conns[0].is_trusted_local()


def test_add_and_list_connection(manager: ConnectionManager) -> None:
    """Add a connection and verify persistence."""
    conn = Connection(
        name="Cloud OpenAI",
        platform_type=PlatformType.OPENAI,
        base_url="https://api.openai.com/v1",
        model_name="gpt-4o",
        is_local=False,
        is_cloud_authorized=True,
    )
    manager.add(conn)

    all_conns = manager.list_all()
    assert len(all_conns) == 2
    assert any(c.name == "Cloud OpenAI" for c in all_conns)


def test_cloud_connection_not_trusted_local(manager: ConnectionManager) -> None:
    """Cloud connections must not be trusted for sensitive tasks."""
    conn = Connection(
        name="Cloud OpenAI",
        platform_type=PlatformType.OPENAI,
        base_url="https://api.openai.com/v1",
        is_local=False,
    )
    manager.add(conn)

    local_conns = manager.list_local()
    assert conn not in local_conns


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:11434", True),
        ("http://[::1]:11434", True),
        ("http://localhost:11434", True),
        ("http://0.0.0.0:11434", False),
        ("http://192.168.1.10:11434", False),
        ("https://api.openai.com/v1", False),
        ("http://0177.0.0.1:11434", False),
    ],
)
def test_is_trusted_local_variants(base_url: str, expected: bool) -> None:
    """Local trust covers loopback, localhost, and rejects non-local hosts."""
    conn = Connection(
        name="Local variant",
        platform_type=PlatformType.OLLAMA,
        base_url=base_url,
        is_local=True,
    )
    assert conn.is_trusted_local() is expected


def test_non_http_scheme_rejected_at_construction() -> None:
    """Non-http(s) schemes are rejected at construction (protocol smuggling guard)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Connection(
            name="Bad scheme",
            platform_type=PlatformType.OLLAMA,
            base_url="ftp://127.0.0.1:11434",
            is_local=True,
        )


def test_update_connection(manager: ConnectionManager) -> None:
    """Update an existing connection."""
    conn = manager.list_all()[0]
    updated = conn.model_copy(update={"model_name": "qwen2.5:14b"})
    manager.update(updated)

    reloaded = ConnectionManager(manager.storage_path)
    assert reloaded.get(conn.id).model_name == "qwen2.5:14b"  # type: ignore[union-attr]


def test_update_unknown_connection_raises_key_error(manager: ConnectionManager) -> None:
    """Updating a non-existent connection raises KeyError."""
    unknown = Connection(
        id=uuid4(),
        name="Unknown",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
    )
    with pytest.raises(KeyError, match="not found"):
        manager.update(unknown)


def test_get_returns_connection_by_id(manager: ConnectionManager) -> None:
    """get fetches a connection by its UUID."""
    conn = manager.list_all()[0]
    assert manager.get(conn.id) == conn


def test_get_missing_connection_returns_none(manager: ConnectionManager) -> None:
    """get returns None for an unknown connection ID."""
    assert manager.get(uuid4()) is None


def test_list_enabled_filters_disabled_connections(manager: ConnectionManager) -> None:
    """list_enabled returns only enabled connections."""
    enabled = Connection(
        name="Enabled",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        is_enabled=True,
    )
    disabled = Connection(
        name="Disabled",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        is_enabled=False,
    )
    manager.add(enabled)
    manager.add(disabled)

    enabled_conns = manager.list_enabled()
    assert enabled in enabled_conns
    assert disabled not in enabled_conns


def test_delete_removes_connection(manager: ConnectionManager) -> None:
    """delete removes a connection and persists the change."""
    conn = manager.list_all()[0]
    manager.delete(conn.id)

    assert manager.get(conn.id) is None
    reloaded = ConnectionManager(manager.storage_path)
    assert reloaded.get(conn.id) is None


def test_delete_unknown_connection_is_no_op(manager: ConnectionManager) -> None:
    """delete silently ignores unknown connection IDs."""
    manager.delete(uuid4())
    assert len(manager.list_all()) == 1


def test_get_default_chat_connection_prefers_enabled_chat(
    manager: ConnectionManager,
) -> None:
    """get_default_chat_connection returns the highest-priority enabled chat connection."""
    chat_conn = Connection(
        name="Chat",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        is_enabled=True,
        capabilities=["chat"],
        priority=20,
    )
    disabled_chat = Connection(
        name="Disabled Chat",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        is_enabled=False,
        capabilities=["chat"],
        priority=30,
    )
    no_chat = Connection(
        name="No Chat",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        capabilities=["embed"],
        priority=30,
    )
    manager.add(chat_conn)
    manager.add(disabled_chat)
    manager.add(no_chat)

    default = manager.get_default_chat_connection()
    assert default == chat_conn


def test_get_default_chat_connection_returns_none_when_unavailable(
    manager: ConnectionManager,
) -> None:
    """get_default_chat_connection returns None when no chat connection is enabled."""
    for conn in manager.list_all():
        conn = conn.model_copy(update={"capabilities": ["embed"]})
        manager.update(conn)

    assert manager.get_default_chat_connection() is None


def test_test_connection_success(tmp_path: Path) -> None:
    """test_connection reports success when the provider is healthy."""
    storage = tmp_path / "connections.json"

    class FakeProvider:
        async def health(self) -> bool:
            return True

        async def close(self) -> None:
            pass

    manager = ConnectionManager(storage, provider_factory=lambda _conn: FakeProvider())
    conn = Connection(
        name="Healthy",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        is_local=True,
    )
    manager.add(conn)

    success, message = manager.test_connection(conn.id)
    assert success is True
    assert "Connected" in message


def test_test_connection_failure(tmp_path: Path) -> None:
    """test_connection reports failure when the provider raises an exception."""
    storage = tmp_path / "connections.json"

    class FakeProvider:
        async def health(self) -> bool:
            raise ConnectionRefusedError("refused")

        async def close(self) -> None:
            pass

    manager = ConnectionManager(storage, provider_factory=lambda _conn: FakeProvider())
    conn = Connection(
        name="Unhealthy",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        is_local=True,
    )
    manager.add(conn)

    success, message = manager.test_connection(conn.id)
    assert success is False
    assert "refused" in message


def test_test_connection_missing_connection(manager: ConnectionManager) -> None:
    """test_connection returns a clear message for unknown connection IDs."""
    success, message = manager.test_connection(uuid4())
    assert success is False
    assert "not found" in message


# --------------------------------------------------------------------------- #
# Regression: AegisAgent must surface the classifier's provider
# --------------------------------------------------------------------------- #
# Bug (fixed): the API server's /clinical/analyze and CDS Hooks endpoints
# read ``getattr(agent, "_llm_provider", None) or getattr(agent, "llm_provider",
# None)``, but AegisAgent only exposed the provider as
# ``agent.classifier.provider``. So the clinical workflow always received
# ``None`` and degraded to rules-only mode on a stock AegisAgent.
#
# The fix adds ``llm_provider`` and ``_llm_provider`` properties to
# AegisAgent that return ``self.classifier.provider``. These tests pin the
# wiring so the regression cannot silently return.


def test_agent_exposes_llm_provider_property(tmp_path: Path) -> None:
    """AegisAgent.llm_provider surfaces the classifier's provider."""
    from doctoragent.config import AegisConfig
    from doctoragent.orchestration.agent import AegisAgent
    from doctoragent.security.master_key import FilePasswordProvider

    cm = ConnectionManager(tmp_path / "connections.json")
    mk = FilePasswordProvider(password="test-pw", storage_path=tmp_path / "mk.bin")
    agent = AegisAgent(
        AegisConfig(), connection_manager=cm, audit_logger=None,
        master_key_provider=mk,
    )
    # The default-seeded Ollama connection gives the classifier a provider.
    prov = agent.llm_provider
    assert prov is not None
    # The _llm_provider alias must resolve to the same object so both
    # getattr lookups in server.py hit the provider.
    assert agent._llm_provider is prov


def test_agent_llm_provider_is_none_when_no_provider(tmp_path: Path) -> None:
    """When the classifier has no provider, the property returns None
    gracefully rather than raising AttributeError."""
    from doctoragent.orchestration.agent import AegisAgent

    class _BareAgent(AegisAgent):
        # Skip the real __init__ (which builds a ConnectionManager + master
        # key provider); we only need the property to be defined and
        # return None when classifier.provider is absent.
        def __init__(self):  # noqa: D401
            self.classifier = None

    agent = _BareAgent()
    assert agent.llm_provider is None
    assert agent._llm_provider is None
