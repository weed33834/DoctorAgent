"""A2A protocol data models (Agent Card / Task / Artifact / TaskStatus).

These mirror the A2A (Google) primitives. They are intentionally dependency-
free (pure dataclasses + ``typing``) so they can be imported by the server,
the client, and API-layer tests without pulling in any network stack.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """A2A Task lifecycle states.

    ``submitted → working → completed | failed | canceled``; a task may also
    pause in ``input-required`` when the agent needs more information from the
    requester (multi-turn interaction).
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class AgentCard:
    """A2A Agent Card — machine-readable capability declaration.

    Served at ``{base_url}/.well-known/agent.json`` and used by
    :class:`~doctoragent.a2a.client.A2AClient` for discovery.
    """

    name: str
    description: str = ""
    url: str = ""
    skills: list[dict[str, Any]] = field(default_factory=list)
    auth_type: str = "none"  # "none" | "bearer" | ...
    endpoints: list[str] = field(default_factory=lambda: ["/a2a/rpc"])
    version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "skills": self.skills,
            "auth_type": self.auth_type,
            "endpoints": self.endpoints,
            "version": self.version,
        }


@dataclass
class A2AArtifact:
    """A task product: text / file / structured data produced by an agent."""

    parts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    index: int = 0


@dataclass
class A2ATask:
    """A task in an agent's lifecycle (submitted → working → terminal)."""

    id: str
    status: TaskStatus = TaskStatus.SUBMITTED
    message: dict[str, Any] | None = None
    artifacts: list[A2AArtifact] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Concatenate the text parts of all artifacts (handy for display)."""
        chunks: list[str] = []
        for artifact in self.artifacts:
            for part in artifact.parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    chunks.append(str(part.get("text", "")))
        return "\n".join(chunks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "message": self.message,
            "artifacts": [
                {"parts": a.parts, "metadata": a.metadata, "index": a.index}
                for a in self.artifacts
            ],
            "metadata": self.metadata,
        }

    @classmethod
    def new(cls, message: dict[str, Any] | None, metadata: dict[str, Any] | None = None) -> "A2ATask":
        """Create a task with a fresh server-generated id."""
        return cls(
            id=f"task-{uuid.uuid4().hex[:12]}",
            status=TaskStatus.SUBMITTED,
            message=message,
            metadata=metadata or {},
        )
