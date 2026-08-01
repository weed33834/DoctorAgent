"""Connection manager for platform configurations."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from doctoragent.connections.models import Connection, PlatformType
from doctoragent.connections.secure_storage import seal_dict, unseal_dict

if TYPE_CHECKING:
    from doctoragent.model.provider import ModelProvider

_logger = logging.getLogger(__name__)

SENSITIVE_FIELDS = {"api_key", "password"}


def _default_ollama_connection() -> Connection:
    """Build the seeded default local Ollama connection."""
    return Connection(
        name="Local Ollama",
        platform_type=PlatformType.OLLAMA,
        base_url="http://127.0.0.1:11434/v1",
        model_name="qwen2.5:7b",
        is_local=True,
        priority=10,
    )


class ConnectionManager:
    """CRUD + test platform connections."""

    def __init__(
        self,
        storage_path: Path,
        provider_factory: Callable[[Connection], ModelProvider] | None = None,
    ) -> None:
        self.storage_path = storage_path
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._connections: dict[UUID, Connection] = {}
        self._provider_factory = provider_factory
        self._lock = threading.RLock()
        self._load()

    def add(self, connection: Connection) -> Connection:
        """Add a new connection."""
        with self._lock:
            self._connections[connection.id] = connection
            self._save()
            return connection

    def get(self, connection_id: UUID) -> Connection | None:
        """Get a connection by ID."""
        with self._lock:
            return self._connections.get(connection_id)

    def list_all(self) -> list[Connection]:
        """Return all connections sorted by priority descending."""
        with self._lock:
            return sorted(self._connections.values(), key=lambda c: c.priority, reverse=True)

    def list_enabled(self) -> list[Connection]:
        """Return enabled connections only."""
        return [c for c in self.list_all() if c.is_enabled]

    def list_local(self) -> list[Connection]:
        """Return enabled local connections."""
        return [c for c in self.list_enabled() if c.is_trusted_local()]

    def update(self, connection: Connection) -> Connection:
        """Update an existing connection."""
        with self._lock:
            if connection.id not in self._connections:
                raise KeyError(f"Connection {connection.id} not found")
            self._connections[connection.id] = connection
            self._save()
            return connection

    def delete(self, connection_id: UUID) -> None:
        """Delete a connection."""
        with self._lock:
            self._connections.pop(connection_id, None)
            self._save()

    def get_default_chat_connection(self, trusted_only: bool = False) -> Connection | None:
        """Return the highest-priority enabled connection capable of chat.

        If *trusted_only* is True, only return trusted local connections,
        suitable for sensitive tasks.
        """
        for conn in self.list_enabled():
            if "chat" not in conn.capabilities:
                continue
            if trusted_only and not conn.is_trusted_local():
                continue
            return conn
        return None

    def _create_provider(self, connection: Connection) -> ModelProvider:
        """Create a model provider for the given connection.

        Uses the injected factory if available; otherwise falls back to the
        global provider registry. The lazy import keeps the platform layer
        decoupled from the model layer when a factory is supplied.
        """
        if self._provider_factory is not None:
            return self._provider_factory(connection)
        from doctoragent.model.provider import create_provider

        return create_provider(connection)

    def test_connection(self, connection_id: UUID) -> tuple[bool, str]:
        """Test a connection synchronously.

        Returns (success, message).
        """
        conn = self.get(connection_id)
        if conn is None:
            return False, "Connection not found"

        provider = self._create_provider(conn)
        try:

            async def _test() -> tuple[bool, str]:
                try:
                    healthy = await provider.health()
                    if healthy:
                        return True, f"Connected to {conn.base_url}"
                    return False, f"No response from {conn.base_url}"
                except Exception as exc:
                    return False, f"Error: {exc}"
                finally:
                    await provider.close()

            return asyncio.run(_test())
        except RuntimeError:
            # 已在事件循环中（asyncio.run 抛 RuntimeError），用新事件循环确保 provider 关闭
            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(provider.close())
                finally:
                    loop.close()
            except Exception:
                _logger.debug("Failed to close provider in async context", exc_info=True)
            return False, "Cannot test connection from async context"

    def _load(self) -> None:
        if not self.storage_path.exists():
            # Seed default local Ollama connection.
            default = _default_ollama_connection()
            self._connections[default.id] = default
            self._save()
            return

        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _logger.warning(
                "Failed to load connections from %s; seeding default config",
                self.storage_path,
            )
            default = _default_ollama_connection()
            self._connections[default.id] = default
            return
        for item in raw.get("connections", []):
            try:
                decrypted = unseal_dict(item, SENSITIVE_FIELDS)
                conn = Connection.model_validate(decrypted)
                self._connections[conn.id] = conn
            except Exception:
                _logger.warning(
                    "Skipping invalid connection entry: %s",
                    item,
                    exc_info=True,
                )

    def _save(self) -> None:
        data: dict[str, Any] = {
            "version": 1,
            "connections": [
                seal_dict(conn.model_dump(), SENSITIVE_FIELDS)
                for conn in self._connections.values()
            ],
        }
        content = json.dumps(data, indent=2, default=str)
        tmp_path = self.storage_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
        tmp_path.replace(self.storage_path)
