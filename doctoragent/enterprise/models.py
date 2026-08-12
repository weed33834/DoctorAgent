"""Enterprise / organization data models (M14).

Pydantic models for the organization & governance layer: organizations and
departments (sub-domain A), user accounts and login events (B/C), budgets and
quotas (F), and platform settings/announcements/maintenance (K). Kept
dependency-free (only pydantic) so they can be imported by the store, service
and API routes without pulling in FastAPI.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from doctoragent.compat import StrEnum


class OrgStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class UserRole(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    ORG_ADMIN = "org_admin"
    MEMBER = "member"
    GUEST = "guest"
    DISABLED = "disabled"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    LOCKED = "locked"


class MFAStatus(StrEnum):
    NOT_ENROLLED = "not_enrolled"
    PENDING = "pending"
    ENROLLED = "enrolled"


class Org(BaseModel):
    id: str
    name: str
    domain: str = ""
    plan: str = "free"
    status: OrgStatus = OrgStatus.ACTIVE
    settings: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class Department(BaseModel):
    id: str
    org_id: str
    name: str
    parent_id: str | None = None
    path: str = ""  # materialized path, e.g. "org/1/2"
    created_at: str = ""


class UserAccount(BaseModel):
    id: str
    org_id: str
    dept_id: str | None = None
    email: str
    display_name: str = ""
    role: UserRole = UserRole.MEMBER
    status: AccountStatus = AccountStatus.ACTIVE
    password_hash: str = ""
    mfa_status: MFAStatus = MFAStatus.NOT_ENROLLED
    mfa_secret: str = ""
    failed_attempts: int = 0
    locked_until: str = ""
    last_login: str = ""
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    created_at: str = ""
    updated_at: str = ""


class LoginEvent(BaseModel):
    id: str
    user_id: str
    org_id: str
    email: str
    ip: str = ""
    user_agent: str = ""
    result: str = "success"  # success | bad_password | locked | disabled | mfa_required | mfa_fail
    detail: str = ""
    at: str = ""


class MFAChallenge(BaseModel):
    user_id: str
    org_id: str
    secret: str
    verified: bool = False
    expires_at: str = ""


class Budget(BaseModel):
    id: str
    scope: str  # org | dept | project | app
    scope_id: str
    period: str = "monthly"
    amount_usd: float = 0.0
    alert_threshold: float = 0.8
    hard_limit: bool = False
    created_at: str = ""


class Quota(BaseModel):
    id: str
    scope: str
    scope_id: str
    tokens_per_day: int = -1  # -1 = unlimited
    calls_per_day: int = -1
    storage_mb: int = -1
    concurrent: int = -1
    updated_at: str = ""


class SystemSetting(BaseModel):
    key: str
    value: str
    updated_at: str = ""


class Announcement(BaseModel):
    id: str
    title: str
    content: str
    level: str = "info"  # info | warn | critical
    pinned: bool = False
    active: bool = True
    created_at: str = ""
    expires_at: str = ""


class MaintenanceState(BaseModel):
    enabled: bool = False
    message: str = ""
    readonly: bool = False
    updated_at: str = ""


class ApiKeyRecord(BaseModel):
    id: str
    org_id: str
    label: str
    prefix: str = ""
    scopes: list[str] = Field(default_factory=lambda: ["*"])
    expires_at: str = ""
    last_used_at: str = ""
    created_at: str = ""
