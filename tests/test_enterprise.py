# mypy: ignore-errors
"""Tests for the enterprise / organization platform (M14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from doctoragent.enterprise import (
    AccountStatus,
    EnterpriseService,
    EnterpriseStore,
    UserRole,
)
from doctoragent.enterprise.security import (
    AccountLockout,
    PasswordPolicy,
    PasswordPolicyError,
    hash_password,
    totp_code,
    verify_password,
    verify_totp,
)


@pytest.fixture
def service(tmp_path: Path) -> EnterpriseService:
    return EnterpriseService(EnterpriseStore(tmp_path / "ent.db"))


# ── security primitives ────────────────────────────────────────────────


def test_password_hash_roundtrip() -> None:
    h = hash_password("Secret123")
    assert verify_password("Secret123", h)
    assert not verify_password("wrong", h)
    assert not verify_password("Secret123", "garbage")


def test_password_policy() -> None:
    policy = PasswordPolicy(min_length=8, require_upper=True, require_digit=True)
    policy.validate("Password123")  # ok
    with pytest.raises(PasswordPolicyError):
        policy.validate("short")
    with pytest.raises(PasswordPolicyError):
        policy.validate("alllowercase1")


def test_totp() -> None:
    from doctoragent.enterprise.security import generate_totp_secret

    secret = generate_totp_secret()
    code = totp_code(secret)
    assert verify_totp(secret, code)
    assert not verify_totp(secret, "000000")


def test_account_lockout() -> None:
    lock = AccountLockout(max_attempts=3, lock_minutes=15)
    locked, _ = lock.is_locked(3, "some-locked-until")
    assert locked
    locked2, _ = lock.is_locked(1, "")
    assert not locked2


# ── service operations ─────────────────────────────────────────────────


def test_org_user_flow(service: EnterpriseService) -> None:
    org = service.create_org("医院A")
    dept = service.create_department(org.id, "心内科")
    user = service.create_user(
        org.id, "doc@hosp.local", "Password123", "张医生", dept.id, UserRole.ORG_ADMIN
    )
    assert user.role == UserRole.ORG_ADMIN
    assert service.store.count_users(org.id) == 1
    # duplicate email rejected
    with pytest.raises(ValueError):
        service.create_user(org.id, "DOC@hosp.local", "Password123")


def test_auth_success_and_mfa(service: EnterpriseService) -> None:
    org = service.create_org("医院B")
    user = service.create_user(org.id, "doc@x.com", "Password123")
    got, result = service.authenticate(org.id, "doc@x.com", "Password123")
    assert result == "success"
    assert got is not None
    # wrong password
    _, r2 = service.authenticate(org.id, "doc@x.com", "Wrong1")
    assert r2 == "bad_password"
    # MFA enroll + verify
    enroll = service.mfa_enroll(user.id)
    assert enroll["secret"]
    code = totp_code(enroll["secret"])
    assert service.mfa_verify_enroll(user.id, code) is True
    # now login requires MFA
    _, r3 = service.authenticate(org.id, "doc@x.com", "Password123")
    assert r3 == "mfa_required"
    assert service.mfa_verify_login(user.id, code) is True


def test_lockout_after_failures(service: EnterpriseService) -> None:
    org = service.create_org("医院C")
    service.create_user(org.id, "doc@x.com", "Password123")
    for _ in range(service.lockout.max_attempts):
        service.authenticate(org.id, "doc@x.com", "Wrong1")
    _, r = service.authenticate(org.id, "doc@x.com", "Password123")
    assert r == "locked"


def test_disabled_user_cannot_login(service: EnterpriseService) -> None:
    org = service.create_org("医院D")
    user = service.create_user(org.id, "doc@x.com", "Password123")
    service.set_user_status(user.id, AccountStatus.DISABLED)
    _, r = service.authenticate(org.id, "doc@x.com", "Password123")
    assert r == "disabled"


def test_bulk_import(service: EnterpriseService) -> None:
    org = service.create_org("医院E")
    res = service.bulk_import_users(
        org.id,
        [
            {"email": "a@x.com", "name": "甲", "password": "Password123"},
            {"email": "b@x.com", "name": "乙", "role": "admin"},
            {"email": "bad-email", "name": "丙"},
        ],
    )
    assert res["created"] == 2
    assert res["failed"] == 1


def test_budget_overlimit(service: EnterpriseService) -> None:
    org = service.create_org("医院F")
    service.set_budget("org", org.id, 100.0, alert_threshold=0.8, hard_limit=True)
    assert service.check_overlimit("org", org.id, 90.0)["action"] == "alert"
    assert service.check_overlimit("org", org.id, 150.0)["action"] == "deny"
    assert service.check_overlimit("org", org.id, 150.0)["exceeded"] is True


def test_api_key_and_announcements(service: EnterpriseService) -> None:
    org = service.create_org("医院G")
    key = service.create_api_key(org.id, "CI")
    assert key["key"].startswith("dk_")
    assert len(service.list_api_keys(org.id)) == 1
    service.create_announcement("维护", "停机", level="warn", pinned=True)
    anns = service.list_announcements()
    assert len(anns) == 1
    assert anns[0].pinned is True


def test_maintenance_roundtrip(service: EnterpriseService) -> None:
    service.set_maintenance(True, "系统升级中", readonly=True)
    m = service.get_maintenance()
    assert m.enabled is True
    assert m.readonly is True
    assert m.message == "系统升级中"


def test_audit_login_events(service: EnterpriseService) -> None:
    org = service.create_org("医院H")
    service.create_user(org.id, "doc@x.com", "Password123")
    service.authenticate(org.id, "doc@x.com", "Password123", ip="1.2.3.4")
    service.authenticate(org.id, "doc@x.com", "Wrong1", ip="1.2.3.4")
    events = service.store.list_login_events(org_id=org.id)
    assert len(events) == 2
    assert any(e.result == "success" for e in events)
    assert any(e.result == "bad_password" for e in events)
