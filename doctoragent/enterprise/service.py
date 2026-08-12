"""Enterprise service facade (M14).

Coordinates the store + security primitives into business operations used by
the API routes: organization/department/user management, MFA enrollment &
verification, login authentication with lockout, budget/quota, announcements,
maintenance and API keys. Emits audit events into the agent's audit logger when
one is provided.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from doctoragent.enterprise.models import (
    AccountStatus,
    Announcement,
    ApiKeyRecord,
    Budget,
    Department,
    LoginEvent,
    MaintenanceState,
    MFAChallenge,
    Org,
    OrgStatus,
    Quota,
    UserAccount,
    UserRole,
)
from doctoragent.enterprise.security import (
    AccountLockout,
    PasswordPolicy,
    generate_totp_secret,
    hash_password,
    totp_provisioning_uri,
    verify_password,
    verify_totp,
)
from doctoragent.enterprise.store import EnterpriseStore, _now


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class EnterpriseService:
    """Facade over the enterprise store and security primitives."""

    def __init__(
        self,
        store: EnterpriseStore,
        password_policy: PasswordPolicy | None = None,
        lockout: AccountLockout | None = None,
        audit_logger: Any | None = None,
    ) -> None:
        self.store = store
        self.policy = password_policy or PasswordPolicy()
        self.lockout = lockout or AccountLockout()
        self.audit_logger = audit_logger

    def _audit(self, event: str, detail: dict[str, Any]) -> None:
        if self.audit_logger is not None:
            try:
                self.audit_logger.log(event, detail)
            except Exception:  # noqa: BLE001 — audit must never break business logic
                pass

    # ── org (A) ─────────────────────────────────────────────────────

    def create_org(self, name: str, domain: str = "", plan: str = "free") -> Org:
        now = _now()
        org = Org(
            id=_id("org"),
            name=name,
            domain=domain,
            plan=plan,
            status=OrgStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self.store.create_org(org)
        self._audit("enterprise.org.created", {"org_id": org.id, "name": name})
        return org

    def list_orgs(self) -> list[Org]:
        return self.store.list_orgs()

    def get_org(self, org_id: str) -> Org | None:
        return self.store.get_org(org_id)

    # ── department (A) ──────────────────────────────────────────────

    def create_department(self, org_id: str, name: str, parent_id: str | None = None) -> Department:
        parent_path = ""
        if parent_id:
            parent = self.store.get_department(parent_id)
            if parent:
                parent_path = parent.path
        dept = Department(
            id=_id("dept"),
            org_id=org_id,
            name=name,
            parent_id=parent_id,
            path=f"{parent_path}/{_id('')}".strip("/"),
            created_at=_now(),
        )
        self.store.create_department(dept)
        return dept

    def list_departments(self, org_id: str) -> list[Department]:
        return self.store.list_departments(org_id)

    def move_department(self, dept_id: str, parent_id: str | None) -> Department:
        dept = self.store.get_department(dept_id)
        if dept is None:
            raise KeyError(f"department {dept_id} not found")
        parent_path = ""
        if parent_id:
            parent = self.store.get_department(parent_id)
            if parent:
                parent_path = parent.path
        dept.parent_id = parent_id
        dept.path = f"{parent_path}/{dept.name}".strip("/")
        self.store.update_department(dept_id, parent_id=parent_id, path=dept.path)
        return dept

    # ── users (B) ───────────────────────────────────────────────────

    def create_user(
        self,
        org_id: str,
        email: str,
        password: str,
        display_name: str = "",
        dept_id: str | None = None,
        role: UserRole = UserRole.MEMBER,
    ) -> UserAccount:
        email = email.lower().strip()
        if self.store.get_user_by_email(org_id, email):
            raise ValueError("用户邮箱已存在")
        self.policy.validate(password)
        now = _now()
        user = UserAccount(
            id=_id("usr"),
            org_id=org_id,
            dept_id=dept_id,
            email=email,
            display_name=display_name,
            role=role,
            status=AccountStatus.ACTIVE,
            password_hash=hash_password(password),
            mfa_status="not_enrolled",
            created_at=now,
            updated_at=now,
        )
        self.store.create_user(user)
        self._audit(
            "enterprise.user.created", {"org_id": org_id, "email": email, "role": role.value}
        )
        return user

    def set_user_status(self, user_id: str, status: AccountStatus) -> UserAccount:
        self.store.update_user(user_id, status=status)
        user = self.store.get_user(user_id)
        self._audit("enterprise.user.status", {"user_id": user_id, "status": status.value})
        return user

    def set_user_role(self, user_id: str, role: UserRole) -> UserAccount:
        self.store.update_user(user_id, role=role)
        self._audit("enterprise.user.role", {"user_id": user_id, "role": role.value})
        return self.store.get_user(user_id)

    def list_users(self, org_id: str | None = None, **filters: Any) -> list[UserAccount]:
        return self.store.list_users(org_id, **filters)

    def bulk_import_users(self, org_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Bulk import users from parsed CSV rows (M14 B.3). Returns results."""
        created, failed = 0, 0
        errors: list[str] = []
        for idx, row in enumerate(rows):
            email = (row.get("email") or "").strip()
            password = row.get("password") or "Welcome@123"
            name = row.get("name") or email
            role = UserRole.MEMBER
            if row.get("role") in ("admin", "org_admin"):
                role = UserRole.ORG_ADMIN
            if not email or "@" not in email:
                failed += 1
                errors.append(f"第{idx + 1}行: 邮箱非法")
                continue
            try:
                self.create_user(org_id, email, password, display_name=name, role=role)
                created += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"第{idx + 1}行 {email}: {exc}")
        return {"created": created, "failed": failed, "errors": errors[:50]}

    # ── authentication (B/C) ────────────────────────────────────────

    def authenticate(
        self,
        org_id: str,
        email: str,
        password: str,
        ip: str = "",
        user_agent: str = "",
    ) -> tuple[UserAccount | None, str]:
        """Verify credentials, honouring lockout/disabled state.

        Returns ``(user, result)`` where ``result`` is one of ``success``,
        ``bad_password``, ``locked``, ``disabled``, ``mfa_required``.
        """
        user = self.store.get_user_by_email(org_id, email.lower())
        if user is None:
            self._record_login(None, org_id, email, ip, user_agent, "bad_password", "unknown user")
            return None, "bad_password"
        locked, _ = self.lockout.is_locked(user.failed_attempts, user.locked_until)
        if locked:
            self._record_login(user, org_id, email, ip, user_agent, "locked", "account locked")
            return None, "locked"
        if user.status == AccountStatus.DISABLED:
            self._record_login(user, org_id, email, ip, user_agent, "disabled", "account disabled")
            return None, "disabled"
        if not verify_password(password, user.password_hash):
            attempts = user.failed_attempts + 1
            locked_until = self.lockout.next_locked_until(attempts)
            self.store.update_user(user.id, failed_attempts=attempts, locked_until=locked_until)
            self._record_login(
                user, org_id, email, ip, user_agent, "bad_password", "wrong password"
            )
            return None, "bad_password"
        # Success — reset failure state.
        self.store.update_user(user.id, failed_attempts=0, locked_until="")
        if user.mfa_status == "enrolled":
            self._record_login(user, org_id, email, ip, user_agent, "mfa_required", "")
            return user, "mfa_required"
        self._record_login(user, org_id, email, ip, user_agent, "success", "")
        self.store.update_user(user.id, last_login=_now())
        return user, "success"

    def _record_login(
        self,
        user: UserAccount | None,
        org_id: str,
        email: str,
        ip: str,
        ua: str,
        result: str,
        detail: str,
    ) -> None:
        self.store.record_login(
            LoginEvent(
                id=_id("login"),
                user_id=user.id if user else "",
                org_id=org_id,
                email=email,
                ip=ip,
                user_agent=ua,
                result=result,
                detail=detail,
                at=_now(),
            )
        )

    # ── MFA (C) ─────────────────────────────────────────────────────

    def mfa_enroll(self, user_id: str, issuer: str = "DoctorAgent") -> dict[str, Any]:
        """Generate a TOTP secret and store a pending challenge. Returns URI + secret."""
        user = self.store.get_user(user_id)
        if user is None:
            raise KeyError("user not found")
        secret = generate_totp_secret()
        self.store.save_mfa_challenge(
            MFAChallenge(
                user_id=user_id,
                org_id=user.org_id,
                secret=secret,
                verified=False,
                expires_at=_now(),
            )
        )
        uri = totp_provisioning_uri(secret, user.email, issuer)
        return {"secret": secret, "provisioning_uri": uri}

    def mfa_verify_enroll(self, user_id: str, code: str) -> bool:
        """Confirm the just-generated TOTP code; on success mark user enrolled."""
        ch = self.store.get_mfa_challenge(user_id)
        if ch is None:
            raise KeyError("no pending MFA enrollment")
        if verify_totp(ch.secret, code):
            self.store.update_user(user_id, mfa_status="enrolled", mfa_secret=ch.secret)
            self.store.delete_mfa_challenge(user_id)
            self._audit("enterprise.mfa.enrolled", {"user_id": user_id})
            return True
        return False

    def mfa_verify_login(self, user_id: str, code: str) -> bool:
        user = self.store.get_user(user_id)
        if user is None or user.mfa_status != "enrolled" or not user.mfa_secret:
            return False
        ok = verify_totp(user.mfa_secret, code)
        if ok:
            self.store.update_user(user_id, last_login=_now())
            self._record_login(user, user.org_id, user.email, "", "", "success", "mfa passed")
        else:
            self._record_login(user, user.org_id, user.email, "", "", "mfa_fail", "bad code")
        return ok

    # ── budget / quota (F) ──────────────────────────────────────────

    def set_budget(
        self,
        scope: str,
        scope_id: str,
        amount_usd: float,
        alert_threshold: float = 0.8,
        hard_limit: bool = False,
    ) -> Budget:
        b = Budget(
            id=_id("bud"),
            scope=scope,
            scope_id=scope_id,
            amount_usd=amount_usd,
            alert_threshold=alert_threshold,
            hard_limit=hard_limit,
            created_at=_now(),
        )
        self.store.upsert_budget(b)
        return b

    def check_overlimit(self, scope: str, scope_id: str, current_usd: float) -> dict[str, Any]:
        """Evaluate a spend amount against the scope budget (M14 F.3)."""
        budget = self.store.get_budget(scope, scope_id)
        if budget is None or budget.amount_usd <= 0:
            return {"exceeded": False, "alert": False, "action": "ok"}
        ratio = current_usd / budget.amount_usd
        action = "ok"
        if budget.hard_limit and ratio >= 1.0:
            action = "deny"
        elif ratio >= budget.alert_threshold:
            action = "alert"
        return {
            "exceeded": ratio >= 1.0,
            "alert": ratio >= budget.alert_threshold,
            "ratio": round(ratio, 3),
            "budget_usd": budget.amount_usd,
            "action": action,
        }

    def set_quota(self, scope: str, scope_id: str, **limits: int) -> Quota:
        q = Quota(
            id=_id("quo"),
            scope=scope,
            scope_id=scope_id,
            tokens_per_day=limits.get("tokens_per_day", -1),
            calls_per_day=limits.get("calls_per_day", -1),
            storage_mb=limits.get("storage_mb", -1),
            concurrent=limits.get("concurrent", -1),
            updated_at=_now(),
        )
        self.store.upsert_quota(q)
        return q

    # ── settings / announcements / maintenance (K) ─────────────────

    def set_settings(self, values: dict[str, str]) -> None:
        for k, v in values.items():
            self.store.set_setting(k, str(v))

    def list_settings(self) -> list[Any]:
        return self.store.list_settings()

    def create_announcement(
        self, title: str, content: str, level: str = "info", pinned: bool = False
    ) -> Announcement:
        a = Announcement(
            id=_id("ann"),
            title=title,
            content=content,
            level=level,
            pinned=pinned,
            active=True,
            created_at=_now(),
        )
        self.store.create_announcement(a)
        return a

    def list_announcements(self, active_only: bool = True) -> list[Announcement]:
        return self.store.list_announcements(active_only=active_only)

    def set_maintenance(
        self, enabled: bool, message: str = "", readonly: bool = False
    ) -> MaintenanceState:
        state = MaintenanceState(
            enabled=enabled, message=message, readonly=readonly, updated_at=_now()
        )
        self.store.set_maintenance(state)
        return state

    def get_maintenance(self) -> MaintenanceState:
        return self.store.get_maintenance()

    # ── API keys (G) ────────────────────────────────────────────────

    def create_api_key(
        self, org_id: str, label: str, scopes: list[str] | None = None
    ) -> dict[str, str]:
        raw = f"dk_{secrets.token_hex(24)}"
        prefix = raw[:12]
        record = ApiKeyRecord(
            id=_id("key"),
            org_id=org_id,
            label=label,
            prefix=prefix,
            scopes=scopes or ["*"],
            created_at=_now(),
        )
        self.store.create_api_key(record)
        # Store a hash for lookup; return the raw key exactly once.
        self.store.set_setting(f"apikey:{record.id}", hash_password(raw))
        return {"id": record.id, "key": raw, "prefix": prefix}

    def list_api_keys(self, org_id: str | None = None) -> list[ApiKeyRecord]:
        return self.store.list_api_keys(org_id)
