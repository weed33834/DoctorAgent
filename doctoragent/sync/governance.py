"""Team sync governance: shared vaults, device role delegation, sync approval flows.

This module is independent of ``sync/auth.py``'s DeviceAuth and focuses on the
governance layer for multi-device shared vaults: shared vault lifecycle, device
role (owner/editor/viewer/approver) delegation, and a SQLite-backed sync
operation approval queue. It can coexist with DeviceAuth.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from doctoragent._utils import atomic_write_text, open_sqlite
from doctoragent.compat import UTC

logger = logging.getLogger(__name__)

# ``fcntl`` provides POSIX cross-process file locking; on Windows, where it is
# unavailable, serialization relies on the in-process RLock only.
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover – non-POSIX platforms
    _fcntl = None  # type: ignore[assignment]


def _now_iso() -> str:
    """ISO 8601 string of the current UTC time."""
    return datetime.now(UTC).isoformat()


def _expires_iso(ttl_hours: int) -> str:
    """ISO 8601 string ttl_hours from now."""
    return (datetime.now(UTC) + timedelta(hours=ttl_hours)).isoformat()


def _atomic_write(path: Path, content: str) -> None:
    """Atomically write a text file with mode 0o600."""
    atomic_write_text(path, content)


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class SharedVault:
    """Shared vault model: a sync container with multiple owners / members."""

    vault_id: str
    name: str
    owner_tenant_id: str
    created_at: str
    member_roles: dict[str, str] = field(default_factory=dict)  # device_id -> role
    is_active: bool = True


@dataclass
class DeviceRole:
    """A device's role record within a shared vault."""

    device_id: str
    role: str  # "owner" | "editor" | "viewer" | "approver"
    vault_id: str
    granted_at: str
    granted_by: str  # authorizer device_id
    permissions: set[str] = field(default_factory=set)


@dataclass
class SyncApprovalRequest:
    """A sync operation approval request."""

    approval_id: str
    vault_id: str
    requester_device_id: str
    operation: str  # "sync_push" | "sync_pull" | "conflict_resolve" | "member_invite"
    operation_details: dict[str, Any]
    status: str = "pending"  # pending / approved / denied / expired
    created_at: str = ""
    decided_at: str | None = None
    decided_by: str | None = None
    expires_at: str = ""


@dataclass
class GovernancePolicy:
    """Governance policy configuration."""

    require_approval_for: set[str] = field(
        default_factory=lambda: {"sync_push", "member_invite", "conflict_resolve"}
    )
    approval_ttl_hours: int = 24
    max_members_per_vault: int = 20
    auto_approve_viewers: bool = True  # viewers are auto-approved on join


# ── RoleRegistry ─────────────────────────────────────────────────────────────


class RoleRegistry:
    """Device role registry: manages device roles within shared vaults."""

    # Role permission matrix
    ROLE_PERMISSIONS: ClassVar[dict[str, set[str]]] = {
        "owner": {
            "sync_push",
            "sync_pull",
            "conflict_resolve",
            "member_invite",
            "member_revoke",
            "delete_vault",
        },
        "editor": {"sync_push", "sync_pull", "conflict_resolve"},
        "viewer": {"sync_pull"},
        "approver": {"sync_pull", "approve_requests"},
    }

    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._roles_file = self._storage_path / "roles.json"
        # Internal state: (vault_id, device_id) -> DeviceRole
        self._roles: dict[tuple[str, str], DeviceRole] = {}
        self._lock = threading.RLock()
        self.load()

    def assign_role(
        self,
        vault_id: str,
        device_id: str,
        role: str,
        granted_by: str,
    ) -> DeviceRole:
        """Assign a role to a device within the given vault."""
        if role not in self.ROLE_PERMISSIONS:
            raise ValueError(f"未知角色: {role}")
        with self._lock:
            permissions = set(self.ROLE_PERMISSIONS[role])
            role_rec = DeviceRole(
                device_id=device_id,
                role=role,
                vault_id=vault_id,
                granted_at=_now_iso(),
                granted_by=granted_by,
                permissions=permissions,
            )
            self._roles[(vault_id, device_id)] = role_rec
            self.save()
            return role_rec

    def revoke_role(self, vault_id: str, device_id: str) -> None:
        """Revoke a device's role within the given vault."""
        with self._lock:
            self._roles.pop((vault_id, device_id), None)
            self.save()

    def get_role(self, vault_id: str, device_id: str) -> DeviceRole | None:
        """Get a device's role record within the given vault."""
        with self._lock:
            return self._roles.get((vault_id, device_id))

    def list_members(self, vault_id: str) -> list[DeviceRole]:
        """List the role records of all members in a vault."""
        with self._lock:
            return [r for (vid, _), r in self._roles.items() if vid == vault_id]

    def has_permission(self, vault_id: str, device_id: str, permission: str) -> bool:
        """Check whether a device holds a permission within a vault."""
        with self._lock:
            role_rec = self._roles.get((vault_id, device_id))
            if role_rec is None:
                return False
            return permission in role_rec.permissions

    def list_vaults_for_device(self, device_id: str) -> list[str]:
        """List all vault_ids a device belongs to."""
        with self._lock:
            return [vid for (vid, did), _ in self._roles.items() if did == device_id]

    def save(self) -> None:
        """Persist the role registry to ``roles.json`` (atomic write, mode 0o600)."""
        with self._lock:
            data = [
                {
                    "device_id": r.device_id,
                    "role": r.role,
                    "vault_id": r.vault_id,
                    "granted_at": r.granted_at,
                    "granted_by": r.granted_by,
                    "permissions": sorted(r.permissions),
                }
                for r in self._roles.values()
            ]
            _atomic_write(self._roles_file, json.dumps(data, indent=2))

    def load(self) -> None:
        """Load role records from ``roles.json``."""
        with self._lock:
            self._roles = {}
            if not self._roles_file.exists():
                return
            try:
                raw = json.loads(self._roles_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("roles.json 读取失败，使用空状态")
                return
            try:
                for entry in raw:
                    key = (entry["vault_id"], entry["device_id"])
                    self._roles[key] = DeviceRole(
                        device_id=entry["device_id"],
                        role=entry["role"],
                        vault_id=entry["vault_id"],
                        granted_at=entry.get("granted_at", ""),
                        granted_by=entry.get("granted_by", ""),
                        permissions=set(entry.get("permissions", [])),
                    )
            except (KeyError, TypeError):
                logger.warning("roles.json 结构错误，使用空状态")
                self._roles = {}


# ── SharedVaultManager ───────────────────────────────────────────────────────


class SharedVaultManager:
    """Shared vault lifecycle management."""

    def __init__(
        self,
        storage_path: Path,
        role_registry: RoleRegistry,
        policy: GovernancePolicy | None = None,
    ) -> None:
        self._storage_path = storage_path
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._vaults_file = self._storage_path / "vaults.json"
        self._role_registry = role_registry
        self._policy = policy or GovernancePolicy()
        self._vaults: dict[str, SharedVault] = {}
        self._lock = threading.RLock()
        self.load()

    def create_vault(
        self,
        name: str,
        owner_tenant_id: str,
        owner_device_id: str,
    ) -> SharedVault:
        """Create a shared vault, assigning the owner role to the owner device."""
        with self._lock:
            vault_id = str(uuid.uuid4())
            vault = SharedVault(
                vault_id=vault_id,
                name=name,
                owner_tenant_id=owner_tenant_id,
                created_at=_now_iso(),
                member_roles={owner_device_id: "owner"},
                is_active=True,
            )
            self._vaults[vault_id] = vault
            self._role_registry.assign_role(vault_id, owner_device_id, "owner", owner_device_id)
            self.save()
            return vault

    def get_vault(self, vault_id: str) -> SharedVault | None:
        """Get the vault with the given id."""
        with self._lock:
            return self._vaults.get(vault_id)

    def list_vaults(self) -> list[SharedVault]:
        """List all vaults."""
        with self._lock:
            return list(self._vaults.values())

    def deactivate_vault(self, vault_id: str, requester_device_id: str) -> None:
        """Deactivate a vault. Only an owner (with delete_vault permission) may do so."""
        with self._lock:
            vault = self._vaults.get(vault_id)
            if vault is None:
                raise KeyError(f"Vault 不存在: {vault_id}")
            if not self._role_registry.has_permission(
                vault_id, requester_device_id, "delete_vault"
            ):
                raise PermissionError("仅 owner 可停用 vault")
            vault.is_active = False
            self.save()

    def invite_member(
        self,
        vault_id: str,
        invitee_device_id: str,
        role: str,
        inviter_device_id: str,
    ) -> SyncApprovalRequest | DeviceRole:
        """Invite a member to join a vault.

        - The inviter must hold the ``member_invite`` permission in the vault.
        - When ``auto_approve_viewers`` is set and ``role == viewer``, the
          invitation is auto-approved (role assigned directly).
        - Otherwise, if ``member_invite`` is in ``require_approval_for``, an
          approval request is returned.
        - Otherwise the role is assigned directly.
        """
        with self._lock:
            vault = self._vaults.get(vault_id)
            if vault is None:
                raise KeyError(f"Vault 不存在: {vault_id}")
            if not vault.is_active:
                raise RuntimeError("Vault 已停用")
            if role not in RoleRegistry.ROLE_PERMISSIONS:
                raise ValueError(f"未知角色: {role}")
            if not self._role_registry.has_permission(vault_id, inviter_device_id, "member_invite"):
                raise PermissionError("邀请方缺少 member_invite 权限")
            members = self._role_registry.list_members(vault_id)
            if len(members) >= self._policy.max_members_per_vault:
                raise RuntimeError(f"Vault 成员数已达上限 {self._policy.max_members_per_vault}")
            # Auto-approve viewers
            if self._policy.auto_approve_viewers and role == "viewer":
                role_rec = self._role_registry.assign_role(
                    vault_id, invitee_device_id, role, inviter_device_id
                )
                vault.member_roles[invitee_device_id] = role
                self.save()
                return role_rec
            # Approval required
            if "member_invite" in self._policy.require_approval_for:
                req = SyncApprovalRequest(
                    approval_id=str(uuid.uuid4()),
                    vault_id=vault_id,
                    requester_device_id=inviter_device_id,
                    operation="member_invite",
                    operation_details={
                        "invitee_device_id": invitee_device_id,
                        "role": role,
                    },
                    status="pending",
                    created_at=_now_iso(),
                    expires_at=_expires_iso(self._policy.approval_ttl_hours),
                )
                return req
            # No approval needed: assign directly
            role_rec = self._role_registry.assign_role(
                vault_id, invitee_device_id, role, inviter_device_id
            )
            vault.member_roles[invitee_device_id] = role
            self.save()
            return role_rec

    def remove_member(self, vault_id: str, device_id: str, requester_device_id: str) -> None:
        """Remove a member.

        An owner (with member_revoke permission) may remove others; members may
        remove themselves.
        """
        with self._lock:
            vault = self._vaults.get(vault_id)
            if vault is None:
                raise KeyError(f"Vault 不存在: {vault_id}")
            is_self = device_id == requester_device_id
            if not is_self and not self._role_registry.has_permission(
                vault_id, requester_device_id, "member_revoke"
            ):
                raise PermissionError("移除他人需要 member_revoke 权限")
            self._role_registry.revoke_role(vault_id, device_id)
            if device_id in vault.member_roles:
                del vault.member_roles[device_id]
            self.save()

    def save(self) -> None:
        """Persist the vault list to ``vaults.json`` (atomic write, mode 0o600)."""
        with self._lock:
            data = [
                {
                    "vault_id": v.vault_id,
                    "name": v.name,
                    "owner_tenant_id": v.owner_tenant_id,
                    "created_at": v.created_at,
                    "member_roles": v.member_roles,
                    "is_active": v.is_active,
                }
                for v in self._vaults.values()
            ]
            _atomic_write(self._vaults_file, json.dumps(data, indent=2))

    def load(self) -> None:
        """Load the vault list from ``vaults.json``."""
        with self._lock:
            self._vaults = {}
            if not self._vaults_file.exists():
                return
            try:
                raw = json.loads(self._vaults_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("vaults.json 读取失败，使用空状态")
                return
            try:
                for entry in raw:
                    vault = SharedVault(
                        vault_id=entry["vault_id"],
                        name=entry["name"],
                        owner_tenant_id=entry["owner_tenant_id"],
                        created_at=entry.get("created_at", ""),
                        member_roles=entry.get("member_roles", {}),
                        is_active=entry.get("is_active", True),
                    )
                    self._vaults[vault.vault_id] = vault
            except (KeyError, TypeError):
                logger.warning("vaults.json 结构错误，使用空状态")
                self._vaults = {}


# ── SyncApprovalQueue ───────────────────────────────────────────────────────


class SyncApprovalQueue:
    """Sync approval queue: persists approval requests in SQLite."""

    def __init__(self, db_path: Path) -> None:
        # ``db_path`` is a directory; the database file is ``sync_approvals.db``
        self._db_dir = db_path
        self._db_dir.mkdir(parents=True, exist_ok=True)
        self._db_file = self._db_dir / "sync_approvals.db"
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """Open a SQLite connection and close it on exit."""
        conn = open_sqlite(self._db_file, row_factory=sqlite3.Row)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create the ``sync_approvals`` table and indexes (idempotent)."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_approvals (
                    approval_id TEXT PRIMARY KEY,
                    vault_id TEXT NOT NULL,
                    requester_device_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    operation_details TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT '',
                    decided_at TEXT,
                    decided_by TEXT,
                    expires_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_approvals_vault ON sync_approvals(vault_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_approvals_requester "
                "ON sync_approvals(requester_device_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_approvals_status ON sync_approvals(status)"
            )
            conn.commit()

    def submit_request(
        self,
        vault_id: str,
        requester_device_id: str,
        operation: str,
        operation_details: dict[str, Any],
        policy: GovernancePolicy,
    ) -> SyncApprovalRequest:
        """Submit an approval request and return the created ``SyncApprovalRequest``."""
        approval_id = str(uuid.uuid4())
        now = _now_iso()
        expires = _expires_iso(policy.approval_ttl_hours)
        details_json = json.dumps(operation_details, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_approvals
                (approval_id, vault_id, requester_device_id, operation,
                 operation_details, status, created_at, decided_at, decided_by, expires_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL, NULL, ?)
                """,
                (
                    approval_id,
                    vault_id,
                    requester_device_id,
                    operation,
                    details_json,
                    now,
                    expires,
                ),
            )
            conn.commit()
        return SyncApprovalRequest(
            approval_id=approval_id,
            vault_id=vault_id,
            requester_device_id=requester_device_id,
            operation=operation,
            operation_details=operation_details,
            status="pending",
            created_at=now,
            decided_at=None,
            decided_by=None,
            expires_at=expires,
        )

    def approve_request(
        self,
        approval_id: str,
        approver_device_id: str,
        role_registry: RoleRegistry,
    ) -> SyncApprovalRequest:
        """Approve a request.

        The approver must hold the ``approve_requests`` permission in the
        vault.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"审批请求不存在: {approval_id}")
            req = self._row_to_request(row)
            if req.status != "pending":
                raise RuntimeError(f"审批请求状态非 pending: {req.status}")
            if self._is_expired(req):
                conn.execute(
                    "UPDATE sync_approvals SET status = 'expired' WHERE approval_id = ?",
                    (approval_id,),
                )
                conn.commit()
                raise RuntimeError("审批请求已过期")
            if not role_registry.has_permission(
                req.vault_id, approver_device_id, "approve_requests"
            ):
                raise PermissionError("批准方缺少 approve_requests 权限")
            now = _now_iso()
            conn.execute(
                """
                UPDATE sync_approvals
                SET status = 'approved', decided_at = ?, decided_by = ?
                WHERE approval_id = ?
                """,
                (now, approver_device_id, approval_id),
            )
            conn.commit()
            req.status = "approved"
            req.decided_at = now
            req.decided_by = approver_device_id
            return req

    def deny_request(
        self,
        approval_id: str,
        denier_device_id: str,
        reason: str = "",
    ) -> SyncApprovalRequest:
        """Deny an approval request."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"审批请求不存在: {approval_id}")
            req = self._row_to_request(row)
            if req.status != "pending":
                raise RuntimeError(f"审批请求状态非 pending: {req.status}")
            now = _now_iso()
            conn.execute(
                """
                UPDATE sync_approvals
                SET status = 'denied', decided_at = ?, decided_by = ?
                WHERE approval_id = ?
                """,
                (now, denier_device_id, approval_id),
            )
            conn.commit()
            req.status = "denied"
            req.decided_at = now
            req.decided_by = denier_device_id
            if reason:
                logger.info("审批请求 %s 被拒绝: %s", approval_id, reason)
            return req

    def list_pending(self, vault_id: str | None = None) -> list[SyncApprovalRequest]:
        """List pending approval requests; optionally filtered by vault."""
        with self._lock, self._connect() as conn:
            if vault_id is None:
                rows = conn.execute(
                    "SELECT * FROM sync_approvals WHERE status = 'pending' ORDER BY created_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sync_approvals WHERE status = 'pending' "
                    "AND vault_id = ? ORDER BY created_at",
                    (vault_id,),
                ).fetchall()
            return [self._row_to_request(r) for r in rows]

    def list_for_device(self, device_id: str) -> list[SyncApprovalRequest]:
        """List all approval requests submitted by a device."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_approvals WHERE requester_device_id = ? ORDER BY created_at",
                (device_id,),
            ).fetchall()
            return [self._row_to_request(r) for r in rows]

    def get_request(self, approval_id: str) -> SyncApprovalRequest | None:
        """Get a single approval request; returns None if it does not exist."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_request(row)

    def cleanup_expired(self) -> int:
        """Clean up expired requests (mark them as expired); returns the number cleaned."""
        now = _now_iso()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE sync_approvals
                SET status = 'expired'
                WHERE status = 'pending' AND expires_at != '' AND expires_at < ?
                """,
                (now,),
            )
            conn.commit()
            return cur.rowcount

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> SyncApprovalRequest:
        """Convert a SQLite row into a ``SyncApprovalRequest``."""
        details_raw = row["operation_details"]
        try:
            details: dict[str, Any] = json.loads(details_raw) if details_raw else {}
        except (json.JSONDecodeError, TypeError):
            details = {}
        return SyncApprovalRequest(
            approval_id=row["approval_id"],
            vault_id=row["vault_id"],
            requester_device_id=row["requester_device_id"],
            operation=row["operation"],
            operation_details=details,
            status=row["status"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
            expires_at=row["expires_at"],
        )

    @staticmethod
    def _is_expired(req: SyncApprovalRequest) -> bool:
        """Determine whether a request has expired."""
        if not req.expires_at:
            return False
        try:
            expires_dt = datetime.fromisoformat(req.expires_at)
        except ValueError:
            return False
        return datetime.now(UTC) > expires_dt


# ── GovernanceManager ───────────────────────────────────────────────────────


class GovernanceManager:
    """Team sync governance entry point.

    Aggregates ``SharedVaultManager`` + ``RoleRegistry`` + the approval queue.
    """

    def __init__(
        self,
        storage_dir: Path,
        policy: GovernancePolicy | None = None,
    ) -> None:
        self._storage_dir = storage_dir
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._policy = policy or GovernancePolicy()
        self._role_registry = RoleRegistry(storage_dir)
        self._vault_manager = SharedVaultManager(storage_dir, self._role_registry, self._policy)
        self._approval_queue = SyncApprovalQueue(storage_dir)

    def create_shared_vault(
        self,
        name: str,
        owner_tenant_id: str,
        owner_device_id: str,
    ) -> SharedVault:
        """Create a shared vault and initialize the owner role."""
        return self._vault_manager.create_vault(name, owner_tenant_id, owner_device_id)

    def request_sync_operation(
        self,
        vault_id: str,
        requester_device_id: str,
        operation: str,
        details: dict[str, Any] | None = None,
    ) -> SyncApprovalRequest | None:
        """Request a sync operation.

        If the operation is in the policy's ``require_approval_for`` set, it is
        enqueued and the approval request is returned; otherwise ``None`` is
        returned to indicate the operation is allowed directly.
        """
        details = details or {}
        if operation not in self._policy.require_approval_for:
            return None
        return self._approval_queue.submit_request(
            vault_id=vault_id,
            requester_device_id=requester_device_id,
            operation=operation,
            operation_details=details,
            policy=self._policy,
        )

    def approve_operation(
        self,
        approval_id: str,
        approver_device_id: str,
    ) -> SyncApprovalRequest:
        """Approve a sync operation approval request."""
        return self._approval_queue.approve_request(
            approval_id, approver_device_id, self._role_registry
        )

    def deny_operation(
        self,
        approval_id: str,
        denier_device_id: str,
        reason: str = "",
    ) -> SyncApprovalRequest:
        """Deny a sync operation approval request."""
        return self._approval_queue.deny_request(approval_id, denier_device_id, reason)

    def check_permission(
        self,
        vault_id: str,
        device_id: str,
        permission: str,
    ) -> bool:
        """Check whether a device holds a permission within a vault."""
        return self._role_registry.has_permission(vault_id, device_id, permission)
