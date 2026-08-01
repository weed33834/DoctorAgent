"""Tests for team sync governance (governance.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from doctoragent.sync.governance import (
    DeviceRole,
    GovernanceManager,
    GovernancePolicy,
    RoleRegistry,
    SharedVaultManager,
    SyncApprovalQueue,
    SyncApprovalRequest,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    """临时治理存储目录。"""
    return tmp_path / "governance"


@pytest.fixture
def role_registry(storage_dir: Path) -> RoleRegistry:
    return RoleRegistry(storage_dir)


@pytest.fixture
def vault_manager(storage_dir: Path, role_registry: RoleRegistry) -> SharedVaultManager:
    return SharedVaultManager(storage_dir, role_registry)


@pytest.fixture
def approval_queue(storage_dir: Path) -> SyncApprovalQueue:
    return SyncApprovalQueue(storage_dir)


@pytest.fixture
def policy() -> GovernancePolicy:
    return GovernancePolicy()


# ── RoleRegistry ─────────────────────────────────────────────────────────────


class TestRoleRegistry:
    def test_role_assign_get(self, role_registry: RoleRegistry) -> None:
        """分配角色并查询。"""
        role_registry.assign_role("v1", "d1", "editor", "owner_d")
        role = role_registry.get_role("v1", "d1")
        assert role is not None
        assert role.device_id == "d1"
        assert role.role == "editor"
        assert role.vault_id == "v1"
        assert role.granted_by == "owner_d"
        assert "sync_push" in role.permissions

    def test_role_revoke(self, role_registry: RoleRegistry) -> None:
        """撤销角色后 get_role 返回 None。"""
        role_registry.assign_role("v1", "d1", "editor", "owner_d")
        assert role_registry.get_role("v1", "d1") is not None
        role_registry.revoke_role("v1", "d1")
        assert role_registry.get_role("v1", "d1") is None

    def test_role_permissions(self) -> None:
        """各角色权限矩阵正确。"""
        perms = RoleRegistry.ROLE_PERMISSIONS
        assert perms["owner"] == {
            "sync_push",
            "sync_pull",
            "conflict_resolve",
            "member_invite",
            "member_revoke",
            "delete_vault",
        }
        assert perms["editor"] == {"sync_push", "sync_pull", "conflict_resolve"}
        assert perms["viewer"] == {"sync_pull"}
        assert perms["approver"] == {"sync_pull", "approve_requests"}

    def test_role_has_permission(self, role_registry: RoleRegistry) -> None:
        """权限检查：owner 拥有全部，viewer 只有 sync_pull。"""
        role_registry.assign_role("v1", "owner_d", "owner", "owner_d")
        role_registry.assign_role("v1", "viewer_d", "viewer", "owner_d")
        assert role_registry.has_permission("v1", "owner_d", "delete_vault")
        assert role_registry.has_permission("v1", "owner_d", "sync_push")
        assert role_registry.has_permission("v1", "viewer_d", "sync_pull")
        assert not role_registry.has_permission("v1", "viewer_d", "sync_push")
        assert not role_registry.has_permission("v1", "viewer_d", "delete_vault")
        # 未知设备无权限
        assert not role_registry.has_permission("v1", "unknown", "sync_pull")

    def test_role_list_vaults_for_device(self, role_registry: RoleRegistry) -> None:
        """列举设备所在 vault。"""
        role_registry.assign_role("v1", "d1", "editor", "owner_d")
        role_registry.assign_role("v2", "d1", "viewer", "owner_d")
        role_registry.assign_role("v1", "d2", "editor", "owner_d")
        vaults = role_registry.list_vaults_for_device("d1")
        assert set(vaults) == {"v1", "v2"}
        assert role_registry.list_vaults_for_device("d2") == ["v1"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions not enforced on Windows")
    def test_role_persist_save_load(self, storage_dir: Path) -> None:
        """保存后重新加载状态保留。"""
        reg1 = RoleRegistry(storage_dir)
        reg1.assign_role("v1", "d1", "editor", "owner_d")
        reg1.assign_role("v2", "d1", "viewer", "owner_d")
        reg1.save()
        # 重新加载
        reg2 = RoleRegistry(storage_dir)
        role = reg2.get_role("v1", "d1")
        assert role is not None
        assert role.role == "editor"
        assert role.granted_by == "owner_d"
        assert role.permissions == {"sync_push", "sync_pull", "conflict_resolve"}
        role2 = reg2.get_role("v2", "d1")
        assert role2 is not None
        assert role2.role == "viewer"
        assert role2.permissions == {"sync_pull"}
        # 文件权限 0o600
        roles_file = storage_dir / "roles.json"
        assert oct(roles_file.stat().st_mode)[-3:] == "600"


# ── SharedVaultManager ───────────────────────────────────────────────────────


class TestSharedVaultManager:
    def test_vault_create(
        self,
        vault_manager: SharedVaultManager,
        role_registry: RoleRegistry,
    ) -> None:
        """创建共享 vault，owner 自动赋角色。"""
        vault = vault_manager.create_vault("team-vault", "tenant_a", "owner_d")
        assert vault.name == "team-vault"
        assert vault.owner_tenant_id == "tenant_a"
        assert vault.is_active is True
        assert vault.member_roles == {"owner_d": "owner"}
        # owner 在角色注册表中有 owner 角色
        role = role_registry.get_role(vault.vault_id, "owner_d")
        assert role is not None
        assert role.role == "owner"
        assert role_registry.has_permission(vault.vault_id, "owner_d", "delete_vault")

    def test_vault_invite_member(self, storage_dir: Path) -> None:
        """邀请成员（无需审批策略下直接分配角色）。"""
        registry = RoleRegistry(storage_dir)
        # 无需审批的策略
        custom_policy = GovernancePolicy(require_approval_for=set())
        mgr = SharedVaultManager(storage_dir, registry, custom_policy)
        vault = mgr.create_vault("v", "t", "owner_d")
        result = mgr.invite_member(vault.vault_id, "editor_d", "editor", "owner_d")
        assert isinstance(result, DeviceRole)
        assert result.device_id == "editor_d"
        assert result.role == "editor"
        assert registry.has_permission(vault.vault_id, "editor_d", "sync_push")

    def test_vault_invite_viewer_auto_approve(
        self,
        vault_manager: SharedVaultManager,
        role_registry: RoleRegistry,
    ) -> None:
        """viewer 自动批准。"""
        vault = vault_manager.create_vault("v", "t", "owner_d")
        result = vault_manager.invite_member(vault.vault_id, "viewer_d", "viewer", "owner_d")
        assert isinstance(result, DeviceRole)
        assert result.role == "viewer"
        assert role_registry.has_permission(vault.vault_id, "viewer_d", "sync_pull")
        assert "viewer_d" in vault.member_roles

    def test_vault_invite_requires_approval(
        self,
        vault_manager: SharedVaultManager,
    ) -> None:
        """非 viewer 角色邀请需审批，返回 SyncApprovalRequest。"""
        vault = vault_manager.create_vault("v", "t", "owner_d")
        result = vault_manager.invite_member(vault.vault_id, "editor_d", "editor", "owner_d")
        assert isinstance(result, SyncApprovalRequest)
        assert result.operation == "member_invite"
        assert result.status == "pending"
        assert result.operation_details["invitee_device_id"] == "editor_d"
        assert result.operation_details["role"] == "editor"
        assert result.expires_at

    def test_vault_remove_member(
        self,
        vault_manager: SharedVaultManager,
        role_registry: RoleRegistry,
    ) -> None:
        """移除成员：owner 可移除他人，成员可移除自己。"""
        vault = vault_manager.create_vault("v", "t", "owner_d")
        # 直接通过 registry 加一个 editor
        role_registry.assign_role(vault.vault_id, "editor_d", "editor", "owner_d")
        vault.member_roles["editor_d"] = "editor"
        # owner 移除 editor
        vault_manager.remove_member(vault.vault_id, "editor_d", "owner_d")
        assert role_registry.get_role(vault.vault_id, "editor_d") is None
        assert "editor_d" not in vault.member_roles
        # 自我移除
        role_registry.assign_role(vault.vault_id, "d2", "viewer", "owner_d")
        vault_manager.remove_member(vault.vault_id, "d2", "d2")
        assert role_registry.get_role(vault.vault_id, "d2") is None

    def test_vault_deactivate_owner_only(self, storage_dir: Path) -> None:
        """仅 owner 可停用 vault。"""
        registry = RoleRegistry(storage_dir)
        custom_policy = GovernancePolicy(require_approval_for=set())
        mgr = SharedVaultManager(storage_dir, registry, custom_policy)
        vault = mgr.create_vault("v", "t", "owner_d")
        # 加一个 editor
        mgr.invite_member(vault.vault_id, "editor_d", "editor", "owner_d")
        # editor 不能停用
        with pytest.raises(PermissionError):
            mgr.deactivate_vault(vault.vault_id, "editor_d")
        assert vault.is_active is True
        # owner 可以停用
        mgr.deactivate_vault(vault.vault_id, "owner_d")
        assert vault.is_active is False


# ── SyncApprovalQueue ────────────────────────────────────────────────────────


class TestSyncApprovalQueue:
    def test_approval_submit(
        self,
        approval_queue: SyncApprovalQueue,
        policy: GovernancePolicy,
    ) -> None:
        """提交审批请求。"""
        req = approval_queue.submit_request("v1", "d1", "sync_push", {"file": "a.txt"}, policy)
        assert req.approval_id
        assert req.vault_id == "v1"
        assert req.requester_device_id == "d1"
        assert req.operation == "sync_push"
        assert req.operation_details == {"file": "a.txt"}
        assert req.status == "pending"
        assert req.created_at
        assert req.expires_at
        assert req.decided_at is None
        assert req.decided_by is None
        # 持久化可查
        loaded = approval_queue.get_request(req.approval_id)
        assert loaded is not None
        assert loaded.approval_id == req.approval_id
        assert loaded.operation_details == {"file": "a.txt"}

    def test_approval_approve(
        self,
        storage_dir: Path,
        policy: GovernancePolicy,
    ) -> None:
        """批准请求需 approver 有 approve_requests 权限。"""
        registry = RoleRegistry(storage_dir)
        registry.assign_role("v1", "approver_d", "approver", "owner_d")
        registry.assign_role("v1", "editor_d", "editor", "owner_d")
        queue = SyncApprovalQueue(storage_dir)
        req = queue.submit_request("v1", "d1", "sync_push", {}, policy)
        # editor 无 approve_requests 权限，不能批准
        with pytest.raises(PermissionError):
            queue.approve_request(req.approval_id, "editor_d", registry)
        # approver 可以批准
        approved = queue.approve_request(req.approval_id, "approver_d", registry)
        assert approved.status == "approved"
        assert approved.decided_by == "approver_d"
        assert approved.decided_at is not None
        # 不能重复批准
        with pytest.raises(RuntimeError):
            queue.approve_request(req.approval_id, "approver_d", registry)

    def test_approval_deny(
        self,
        approval_queue: SyncApprovalQueue,
        policy: GovernancePolicy,
    ) -> None:
        """拒绝请求。"""
        req = approval_queue.submit_request("v1", "d1", "sync_push", {}, policy)
        denied = approval_queue.deny_request(req.approval_id, "approver_d", "不需要")
        assert denied.status == "denied"
        assert denied.decided_by == "approver_d"
        assert denied.decided_at is not None
        # 不能重复处理
        with pytest.raises(RuntimeError):
            approval_queue.deny_request(req.approval_id, "approver_d")

    def test_approval_list_pending(
        self,
        approval_queue: SyncApprovalQueue,
        policy: GovernancePolicy,
    ) -> None:
        """列举待审批。"""
        r1 = approval_queue.submit_request("v1", "d1", "sync_push", {}, policy)
        r2 = approval_queue.submit_request("v1", "d2", "sync_push", {}, policy)
        r3 = approval_queue.submit_request("v2", "d1", "sync_push", {}, policy)
        # 全部 pending
        all_pending = approval_queue.list_pending()
        assert len(all_pending) == 3
        # 按 vault 过滤
        v1_pending = approval_queue.list_pending("v1")
        assert len(v1_pending) == 2
        assert {p.approval_id for p in v1_pending} == {r1.approval_id, r2.approval_id}
        v2_pending = approval_queue.list_pending("v2")
        assert len(v2_pending) == 1
        assert v2_pending[0].approval_id == r3.approval_id
        # 拒绝一个后 pending 减少
        approval_queue.deny_request(r1.approval_id, "approver_d")
        assert len(approval_queue.list_pending()) == 2
        # list_for_device
        assert len(approval_queue.list_for_device("d1")) == 2

    def test_approval_cleanup_expired(
        self,
        approval_queue: SyncApprovalQueue,
        policy: GovernancePolicy,
    ) -> None:
        """清理过期请求。"""
        req = approval_queue.submit_request("v1", "d1", "sync_push", {}, policy)
        # 直接修改 expires_at 为过去时间
        with approval_queue._connect() as conn:
            conn.execute(
                "UPDATE sync_approvals SET expires_at = ? WHERE approval_id = ?",
                ("2020-01-01T00:00:00+00:00", req.approval_id),
            )
            conn.commit()
        cleaned = approval_queue.cleanup_expired()
        assert cleaned == 1
        updated = approval_queue.get_request(req.approval_id)
        assert updated is not None
        assert updated.status == "expired"
        # 再次清理无变化
        assert approval_queue.cleanup_expired() == 0


# ── GovernanceManager ────────────────────────────────────────────────────────


class TestGovernanceManager:
    def test_governance_create_vault(self, storage_dir: Path) -> None:
        """综合创建。"""
        gm = GovernanceManager(storage_dir)
        vault = gm.create_shared_vault("team", "tenant_a", "owner_d")
        assert vault.name == "team"
        assert gm.check_permission(vault.vault_id, "owner_d", "delete_vault")
        assert gm.check_permission(vault.vault_id, "owner_d", "sync_push")

    def test_governance_request_sync_with_approval(self, storage_dir: Path) -> None:
        """需审批的同步操作（sync_push）。"""
        gm = GovernanceManager(storage_dir)
        vault = gm.create_shared_vault("v", "t", "owner_d")
        req = gm.request_sync_operation(vault.vault_id, "owner_d", "sync_push", {"file": "a.txt"})
        assert req is not None
        assert req.operation == "sync_push"
        assert req.status == "pending"
        # 队列可查
        pending = gm._approval_queue.list_pending(vault.vault_id)
        assert len(pending) == 1

    def test_governance_request_sync_without_approval(self, storage_dir: Path) -> None:
        """不需审批的同步操作（sync_pull）直接允许。"""
        gm = GovernanceManager(storage_dir)
        vault = gm.create_shared_vault("v", "t", "owner_d")
        result = gm.request_sync_operation(vault.vault_id, "owner_d", "sync_pull")
        assert result is None
        # 队列空
        assert gm._approval_queue.list_pending() == []

    def test_governance_check_permission(self, storage_dir: Path) -> None:
        """权限检查。"""
        gm = GovernanceManager(storage_dir)
        vault = gm.create_shared_vault("v", "t", "owner_d")
        # owner 有全部权限
        assert gm.check_permission(vault.vault_id, "owner_d", "sync_push")
        assert gm.check_permission(vault.vault_id, "owner_d", "member_invite")
        # 未知设备无权限
        assert not gm.check_permission(vault.vault_id, "unknown_d", "sync_pull")
