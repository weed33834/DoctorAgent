"""Agent modernisation package: checkpoint persistence + MCP server bridge.

Public API:

- :class:`CheckpointStore` — SQLite-backed save/load of agent run snapshots.
- :func:`build_mcp_server` / :func:`run_mcp_server` — expose the agent's
  tool registry over the Model Context Protocol.

The streaming output (``Agent.run_stream``) and checkpoint hooks
(``Agent.save_checkpoint`` / ``Agent.resume_from_checkpoint``) live on
:class:`~doctoragent.model.agent.Agent` itself to keep the existing run logic
untouched.
"""

from __future__ import annotations

from doctoragent.agent.checkpoint import AgentCheckpoint, CheckpointStore
from doctoragent.agent.mcp_server import build_mcp_server, run_mcp_server

__all__ = [
    "AgentCheckpoint",
    "CheckpointStore",
    "build_mcp_server",
    "run_mcp_server",
]
