"""A2A (Agent-to-Agent) protocol support.

Implements the Agent-to-Agent protocol (Google, Apr 2025) — the industry
companion to MCP: *MCP gives an agent hands, A2A gives it colleagues.*

Three primitives are provided:

* :class:`~doctoragent.a2a.models.AgentCard` — capability declaration served
  at ``/.well-known/agent.json`` for discovery.
* :class:`~doctoragent.a2a.server.A2AServer` — JSON-RPC 2.0 task server
  exposing ``task/send``, ``task/get``, ``task/cancel`` and ``agents/list``.
* :class:`~doctoragent.a2a.client.A2AClient` — discovers remote agents via
  their Agent Card and submits/polls tasks.

The server lives in the **L8 API Service Layer** (``/.well-known/agent.json``
+ ``POST /a2a/rpc``) and the client in the **L7 Orchestration Layer**; together
they let DoctorAgent collaborate with external agents across frameworks.
"""

from __future__ import annotations

from doctoragent.a2a.client import A2AClient
from doctoragent.a2a.models import A2AArtifact, A2ATask, AgentCard, TaskStatus
from doctoragent.a2a.server import (
    A2AError,
    A2AServer,
    build_default_handler,
)

__all__ = [
    "A2AArtifact",
    "A2AClient",
    "A2AError",
    "A2AServer",
    "A2ATask",
    "AgentCard",
    "TaskStatus",
    "build_default_handler",
]
