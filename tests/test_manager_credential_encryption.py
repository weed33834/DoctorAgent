"""Tests for connection credential encryption persistence."""

from pathlib import Path

import pytest

from doctoragent.connections.manager import ConnectionManager
from doctoragent.connections.models import AuthMethod, Connection, PlatformType


@pytest.fixture
def isolated_storage_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use a temporary storage key file so tests do not touch the user's home."""
    key_path = tmp_path / "storage.key"
    monkeypatch.setenv("DOCTORAGENT_STORAGE_KEY_FILE", str(key_path))
    return key_path


def _find_connection_by_name(manager: ConnectionManager, name: str) -> Connection | None:
    """Helper to fetch a connection by name."""
    for conn in manager.list_all():
        if conn.name == name:
            return conn
    return None


def test_api_key_is_sealed_on_disk(tmp_path: Path, isolated_storage_key: Path) -> None:
    """API key must be sealed (prefixed) in the connections file."""
    storage = tmp_path / "connections.json"
    manager = ConnectionManager(storage)
    manager.add(
        Connection(
            name="Cloud OpenAI",
            platform_type=PlatformType.OPENAI,
            base_url="https://api.openai.com/v1",
            auth_method=AuthMethod.BEARER,
            api_key="sk-secret-12345",
            is_local=False,
            is_cloud_authorized=True,
        )
    )

    raw_text = storage.read_text(encoding="utf-8")
    assert "aes:" in raw_text or "dpapi:" in raw_text


def test_api_key_roundtrip(tmp_path: Path, isolated_storage_key: Path) -> None:
    """API key is decrypted correctly when loading the manager."""
    storage = tmp_path / "connections.json"
    manager = ConnectionManager(storage)
    manager.add(
        Connection(
            name="Cloud OpenAI",
            platform_type=PlatformType.OPENAI,
            base_url="https://api.openai.com/v1",
            auth_method=AuthMethod.BEARER,
            api_key="sk-secret-12345",
            is_local=False,
            is_cloud_authorized=True,
        )
    )

    reloaded = ConnectionManager(storage)
    conn = _find_connection_by_name(reloaded, "Cloud OpenAI")
    assert conn is not None
    assert conn.api_key.get_secret_value() == "sk-secret-12345"
