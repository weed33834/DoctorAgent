"""Multi-tenant management module (Phase 9.1 data-layer isolation).

Each tenant owns an independent master key storage directory; the tenant
registry is persisted at ``base_storage_dir/tenants.json``. The default
tenant ``default`` is created lazily on first access, preserving backward
compatibility with legacy single-tenant deployments.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from doctoragent.compat import UTC
from doctoragent.security.crypto import _atomic_write_bytes
from doctoragent.security.master_key import create_master_key_provider

# Supported master key provider types.
# NOTE: must match the names recognised by
# ``master_key.create_master_key_provider`` (e.g. ``mac-keychain``).
_SUPPORTED_KEY_PROVIDERS = {"filepassword", "mac-keychain", "dpapi", "tpm"}

# Valid tenant_id characters: 1-64 of [A-Za-z0-9._-]. This rejects path
# separators, ``..`` traversal, and empty/oversized identifiers.
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class TenantInfo:
    """Metadata for a single tenant."""

    tenant_id: str
    name: str
    created_at: str
    key_provider_type: str  # "filepassword" | "mac-keychain" | "dpapi" | "tpm"
    storage_path: str  # master key storage path for this tenant
    is_active: bool = True


class TenantManager:
    """Multi-tenant management: create, list, deactivate tenants, and route keys per tenant."""

    DEFAULT_TENANT_ID = "default"

    def __init__(self, base_storage_dir: Path) -> None:
        """Initialize the tenant manager.

        ``base_storage_dir`` is the root directory where each tenant's master
        key is stored; the tenant registry lives at
        ``base_storage_dir/tenants.json``.
        """
        self._base_dir = Path(base_storage_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._base_dir / "tenants.json"

    # ── Registry read/write ───────────────────────────────────────────

    def _load_registry(self) -> dict[str, dict[str, object]]:
        """Read the tenant registry; returns an empty dict if missing or corrupt."""
        if not self._registry_path.exists():
            return {}
        try:
            raw = self._registry_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Keep only entries whose value is a dict, so dirty data does not break parsing.
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save_registry(self, registry: dict[str, dict[str, object]]) -> None:
        """Atomically write the tenant registry with mode 0o600."""
        payload = json.dumps(registry, indent=2, ensure_ascii=False).encode("utf-8")
        _atomic_write_bytes(self._registry_path, payload)
        # _atomic_write_bytes already writes with 0o600; this idempotently
        # tightens the permissions.
        with contextlib.suppress(OSError):
            os.chmod(self._registry_path, stat.S_IRUSR | stat.S_IWUSR)

    @staticmethod
    def _row_to_info(tenant_id: str, row: dict[str, object]) -> TenantInfo:
        """Build a TenantInfo from a registry row, using safe defaults for missing fields."""
        return TenantInfo(
            tenant_id=tenant_id,
            name=str(row.get("name", tenant_id)),
            created_at=str(row.get("created_at", "")),
            key_provider_type=str(row.get("key_provider_type", "filepassword")),
            storage_path=str(row.get("storage_path", "")),
            is_active=bool(row.get("is_active", True)),
        )

    # ── Public API ────────────────────────────────────────────────────

    def _validate_tenant_id(self, tenant_id: str) -> None:
        """Validate ``tenant_id`` to prevent path traversal and injection.

        Allows 1-64 chars of ``[A-Za-z0-9._-]`` (rejects path separators and
        ``..``), and confirms the resolved storage path stays within
        ``base_dir``. Raises :class:`ValueError` on any violation.
        """
        if not isinstance(tenant_id, str):
            raise ValueError(f"非法 tenant_id: {tenant_id!r}")
        # Reject path-like and traversal identifiers before the charset check
        # (the regex permits "."/"..", which would resolve to base_dir).
        if tenant_id in (".", "..") or "/" in tenant_id or "\\" in tenant_id:
            raise ValueError(f"非法 tenant_id: {tenant_id!r}")
        if not _TENANT_ID_RE.match(tenant_id):
            raise ValueError(f"非法 tenant_id: {tenant_id!r}")
        # Defence in depth: even with a charset restriction, confirm the
        # resolved path cannot escape base_dir (e.g. via ``..``).
        base = self._base_dir.resolve()
        storage = (base / tenant_id).resolve()
        if storage != base and base not in storage.parents:
            raise ValueError(f"tenant_id 路径越界: {tenant_id!r}")

    def create_tenant(
        self,
        tenant_id: str,
        name: str,
        key_provider_type: str = "filepassword",
        password: str | None = None,
    ) -> TenantInfo:
        """Create a new tenant and initialize its master key storage.

        Idempotent: if the tenant already exists, the existing record is
        returned directly (the key is not re-initialized).
        """
        self._validate_tenant_id(tenant_id)
        if not tenant_id:
            raise ValueError("tenant_id 不能为空")
        key_provider_type = key_provider_type.lower()
        if key_provider_type not in _SUPPORTED_KEY_PROVIDERS:
            raise ValueError(
                f"不支持的 key_provider_type: {key_provider_type}; "
                f"支持的有: {sorted(_SUPPORTED_KEY_PROVIDERS)}"
            )

        registry = self._load_registry()
        existing = registry.get(tenant_id)
        if existing is not None:
            return self._row_to_info(tenant_id, existing)

        storage_path = self.get_storage_path(tenant_id)
        storage_path.mkdir(parents=True, exist_ok=True)
        # Initialize this tenant's master key (lazy first derivation/write).
        provider = create_master_key_provider(
            key_provider_type,
            storage_path,
            password=password,
        )
        # Trigger key generation and persistence (FilePassword etc. will write
        # salt/kdf files). When filepassword is given no password (or the
        # current platform does not support the chosen provider), get_key
        # raises RuntimeError; in that case registration still completes and
        # the key is left to be initialized on first real use.
        key: bytearray | None = None
        try:
            key = bytearray(provider.get_key())
        except (RuntimeError, NotImplementedError):
            key = None
        finally:
            if key is not None:
                # Secure wipe: minimize the master key's lifetime in memory.
                for i in range(len(key)):
                    key[i] = 0
            provider.clear()

        info = TenantInfo(
            tenant_id=tenant_id,
            name=name,
            created_at=datetime.now(UTC).isoformat(),
            key_provider_type=key_provider_type,
            storage_path=str(storage_path),
            is_active=True,
        )
        registry[tenant_id] = asdict(info)
        self._save_registry(registry)
        return info

    def get_tenant(self, tenant_id: str) -> TenantInfo | None:
        """Return tenant info; returns None if it does not exist.

        The default tenant ``default`` is created lazily: it is initialized on
        first access.
        """
        self._validate_tenant_id(tenant_id)
        if tenant_id == self.DEFAULT_TENANT_ID:
            self._ensure_default_tenant()
        registry = self._load_registry()
        row = registry.get(tenant_id)
        if row is None:
            return None
        return self._row_to_info(tenant_id, row)

    def list_tenants(self) -> list[TenantInfo]:
        """List all tenants (including deactivated ones)."""
        # Ensure the default tenant exists (lazy creation).
        self._ensure_default_tenant()
        registry = self._load_registry()
        return [self._row_to_info(tid, row) for tid, row in registry.items()]

    def deactivate_tenant(self, tenant_id: str) -> None:
        """Deactivate a tenant (soft deactivation).

        Keeps the registry record but marks ``is_active=False``.
        """
        self._validate_tenant_id(tenant_id)
        if tenant_id == self.DEFAULT_TENANT_ID:
            raise ValueError("默认租户不能被禁用")
        registry = self._load_registry()
        row = registry.get(tenant_id)
        if row is None:
            raise KeyError(f"租户不存在: {tenant_id}")
        row["is_active"] = False
        registry[tenant_id] = row
        self._save_registry(registry)

    def get_storage_path(self, tenant_id: str) -> Path:
        """Return the master key storage path for a tenant."""
        self._validate_tenant_id(tenant_id)
        return self._base_dir / tenant_id

    def tenant_exists(self, tenant_id: str) -> bool:
        """Determine whether a tenant is registered."""
        self._validate_tenant_id(tenant_id)
        if tenant_id == self.DEFAULT_TENANT_ID:
            self._ensure_default_tenant()
        return tenant_id in self._load_registry()

    # ── Internal helpers ──────────────────────────────────────────────

    def _ensure_default_tenant(self) -> None:
        """Lazily create the default tenant (if not yet registered)."""
        registry = self._load_registry()
        if self.DEFAULT_TENANT_ID in registry:
            return
        storage_path = self.get_storage_path(self.DEFAULT_TENANT_ID)
        storage_path.mkdir(parents=True, exist_ok=True)
        info = TenantInfo(
            tenant_id=self.DEFAULT_TENANT_ID,
            name="Default Tenant",
            created_at=datetime.now(UTC).isoformat(),
            key_provider_type="filepassword",
            storage_path=str(storage_path),
            is_active=True,
        )
        registry[self.DEFAULT_TENANT_ID] = asdict(info)
        self._save_registry(registry)
