"""Agent interoperability (M27).

Models for cross-agent interop: external agent directory entries, trust
levels, interop policies, and A2A task monitoring records. Dependency-free
(pydantic only).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from doctoragent.compat import StrEnum


class TrustLevel(StrEnum):
    UNTRUSTED = "untrusted"
    LIMITED = "limited"
    TRUSTED = "trusted"
    HIGH = "high"


class ExternalAgent(BaseModel):
    id: str
    name: str
    url: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    trust_level: TrustLevel = TrustLevel.LIMITED
    auth_type: str = "none"
    health: str = "unknown"  # unknown | online | offline
    last_seen: str = ""
    created_at: str = ""


class InteropPolicy(BaseModel):
    id: str
    name: str
    allow_agents: list[str] = Field(default_factory=list)  # empty = any
    deny_actions: list[str] = Field(default_factory=list)
    rate_limit_per_min: int = 60
    require_trust: TrustLevel = TrustLevel.TRUSTED
    audit_level: str = "full"  # none | summary | full
    enabled: bool = True
    created_at: str = ""


class A2ATaskRecord(BaseModel):
    id: str
    peer_agent: str
    direction: str = "outbound"  # outbound | inbound
    status: str = "submitted"
    message_summary: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    error: str = ""
