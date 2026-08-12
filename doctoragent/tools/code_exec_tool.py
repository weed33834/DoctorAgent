"""Sandboxed code-execution tool (M4.4 / M4.5).

A :class:`~doctoragent.model.tools.Tool` that runs Python code inside the
DoctorAgent sandbox and returns stdout + stderr + (optionally) a generated
image (PNG/SVG) as a base64 data URL so the chat can display charts/plots.

This is what lets a user say *"用 Python 画个柱状图"* in the conversation and
get a rendered image back — real, sandboxed execution, not a stub.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}


class CodeExecTool(Tool):
    """Run Python code in the sandbox; returns stdout + optional generated image."""

    def __init__(self, sandbox: Any | None = None, timeout: float = 30.0) -> None:
        self.sandbox = sandbox
        self.timeout = timeout

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="code_exec",
            description=(
                "Execute Python code in an isolated sandbox. Useful for data "
                "analysis, calculation, and generating charts/plots. If the "
                "code saves an image file (e.g. chart.png / chart.svg) into "
                "the working directory, it is returned as a data URL so the "
                "chart is displayed in the conversation."
            ),
            parameters=[
                ToolParameter(
                    name="code",
                    type="string",
                    required=True,
                    description="Python source code to execute",
                ),
                ToolParameter(
                    name="timeout",
                    type="integer",
                    required=False,
                    description="Max execution seconds",
                    default=30,
                ),
            ],
            category="code",
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        code = kwargs.get("code", "")
        if not code.strip():
            return ToolResult(success=False, error="empty code", tool_name="code_exec")
        # Fail-closed: refuse to run untrusted code unless a real OS-level
        # confinement backend is available (bwrap / effective unshare+chroot /
        # container). Otherwise a bare subprocess can read host files such as
        # /etc/passwd. Operators on trusted hosts may force unsafe mode with
        # DOCTORAGENT_ALLOW_UNSAFE_CODE=1.
        if not self._isolation_ok():
            return ToolResult(
                success=False,
                error=(
                    "code execution refused: no secure OS sandbox available "
                    "(needs bwrap / chroot / container). On a trusted host set "
                    "DOCTORAGENT_ALLOW_UNSAFE_CODE=1 to force unsafe subprocess mode."
                ),
                tool_name="code_exec",
            )
        timeout = float(kwargs.get("timeout") or self.timeout)
        try:
            result = self._run(code, timeout)
        except Exception as exc:  # noqa: BLE001
            logger.exception("code_exec failed")
            return ToolResult(success=False, error=str(exc), tool_name="code_exec")

        data: dict[str, Any] = {
            "returncode": result.returncode,
            "stdout": result.stdout[-6000:],
            "stderr": result.stderr[-4000:],
            "isolation": result.isolation_level,
        }
        # Capture a generated image if the code produced one.
        image = self._capture_image()
        if image:
            data["image"] = image
        if not result.ok:
            return ToolResult(
                success=False,
                error=f"exit {result.returncode}: {result.stderr[-800:]}",
                data=data,
                tool_name="code_exec",
            )
        return ToolResult(success=True, data=data, tool_name="code_exec")

    @staticmethod
    def _isolation_ok() -> bool:
        if os.environ.get("DOCTORAGENT_ALLOW_UNSAFE_CODE") == "1":
            return True
        try:
            from doctoragent.security.sandbox import SandboxManager

            return SandboxManager.isolation_effective()
        except Exception:  # noqa: BLE001
            return False

    def _run(self, code: str, timeout: float) -> Any:
        import sys

        if self.sandbox is None:
            from doctoragent.security.sandbox import SandboxManager

            self.sandbox = SandboxManager(enable_strong_isolation=True)
        work = Path(self.sandbox.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        script = work / "code.py"
        script.write_text(code, encoding="utf-8")
        # Use the interpreter's absolute path (the sandbox forces a minimal
        # PATH, so a bare "python" may not resolve).
        python = sys.executable or "python3"
        return self.sandbox.run_sandboxed([python, "-u", "code.py"], timeout=timeout)

    def _capture_image(self) -> str | None:
        """Return the first generated image in the sandbox work dir as data URL."""
        work = Path(self.sandbox.work_dir)
        if not work.is_dir():
            return None
        for f in sorted(work.iterdir()):
            if f.is_file() and f.suffix.lower() in _IMAGE_EXTS:
                try:
                    mime = {
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".gif": "image/gif",
                        ".svg": "image/svg+xml",
                        ".webp": "image/webp",
                    }[f.suffix.lower()]
                    b64 = base64.b64encode(f.read_bytes()).decode()
                    return f"data:{mime};base64,{b64}"
                except Exception:  # noqa: BLE001
                    continue
        return None


def register_code_exec_tool(registry: Any, sandbox: Any | None = None) -> str | None:
    """Register :class:`CodeExecTool` into *registry*; returns the tool name."""
    name = "code_exec"
    if registry.get(name) is None:
        registry.register(CodeExecTool(sandbox=sandbox))
    return name
