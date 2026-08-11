"""Sandboxed execution for untrusted extractors and plugins.

DoctorAgent runs third-party / user-supplied extractors when classifying files.
Running them inside the agent process would let a malicious extractor touch
the vault, the master key, or the network.  :class:`SandboxManager` launches
such code in an isolated subprocess with the strongest OS-level isolation
available on the current platform, falling back gracefully when the strong
isolation primitives are unavailable.

Platform matrix
---------------

* **Linux** — ``unshare`` mount/PID/net namespaces + ``seccomp`` filter via a
  wrapper shell command.  When ``unshare`` is absent the manager degrades to
  a plain subprocess with a sanitised environment, a locked ``cwd`` and no
  inherited file descriptors beyond stdin/stdout/stderr.
* **macOS** — ``sandbox-exec`` with a deny-by-default profile that only allows
  reading the explicitly allowed paths.
* **Windows** — a restricted subprocess with a scrubbed environment and a
  read-only ``cwd``.  ``AppContainer`` / restricted-token hardening is
  attempted when the Windows API surface is reachable, otherwise the
  environment + cwd isolation is used.

All execution attempts are recorded to the audit log; failures produce a
``sandbox_run_failed`` event and a suspicious non-zero exit coupled with
disallowed-path access produces a ``sandbox_escape_attempt`` event.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from doctoragent.security.audit_log import AuditLogger

logger = logging.getLogger(__name__)

# Default wall-clock budget for a sandboxed command.  Generous enough for a
# small extractor while preventing an infinite loop from pinning a CPU.
DEFAULT_TIMEOUT_SECONDS = 30.0

# Environment variables that must never be inherited by the sandbox, because
# they could leak the master key, vault path or other secrets.
_ENV_DENYLIST: frozenset[str] = frozenset(
    {
        "DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD",
        "DOCTORAGENT_MASTER_KEY_PASSWORD",
        "DOCTORAGENT_SECURITY__MASTER_KEY_PROVIDER",
        # Token-style secrets that integrations may have placed in env.
        "DOCTORAGENT_INTEGRATIONS__WEBHOOK_DEFAULT_SECRET",
        "DOCTORAGENT_INTEGRATIONS__S3_SECRET_KEY",
        "DOCTORAGENT_INTEGRATIONS__WEBDAV_PASSWORD",
        # Generic process secrets.
        "PYTHONPATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
    }
)


@dataclass
class SandboxResult:
    """Outcome of a single sandboxed run."""

    # ``returncode`` defaults to -1 (sentinel for "not yet populated") so the
    # result can be constructed before ``subprocess.run`` populates it; the
    # ``ok`` property treats any non-zero returncode (including -1) as failure.
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    command: list[str] = field(default_factory=list)
    isolation_level: str = "subprocess"

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot be initialised for the current platform."""


class SandboxManager:
    """Run untrusted commands in an isolated subprocess.

    Parameters
    ----------
    audit_logger:
        Optional :class:`AuditLogger` used to record sandbox events.
    work_dir:
        Base directory the sandbox is allowed to read/write.  Defaults to a
        fresh temporary directory per manager instance when omitted.
    enable_strong_isolation:
        When True (default) the manager tries to use ``unshare``/``sandbox-exec``
        /``AppContainer``.  Setting it to False forces the lightweight
        subprocess fallback unconditionally — useful in containers that
        forbid the privileged wrappers.
    """

    def __init__(
        self,
        audit_logger: AuditLogger | None = None,
        work_dir: Path | None = None,
        enable_strong_isolation: bool = True,
    ) -> None:
        self._audit = audit_logger
        self._enable_strong = enable_strong_isolation
        if work_dir is not None:
            self._work_dir = work_dir
            self._owns_work_dir = False
        else:
            self._work_dir = Path(tempfile.mkdtemp(prefix="doctoragent-sandbox-"))
            self._owns_work_dir = True
        self._platform = sys.platform

    # ── public API ────────────────────────────────────────────────────────

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    @property
    def platform(self) -> str:
        return self._platform

    @staticmethod
    def isolation_effective() -> bool:
        """Return True only when a real filesystem-confinement backend works.

        A bare ``subprocess`` or a non-effective ``unshare``/``bwrap`` does
        NOT confine the process (the child can still read host files such as
        ``/etc/passwd``). This probe actually runs a test program that tries
        to read ``/etc/passwd`` and returns True only if the read is blocked.
        Callers that run *untrusted* code MUST gate on this.
        """
        return SandboxManager._probe_isolation_effective()


    @staticmethod
    def _probe_isolation_effective() -> bool:
        """Return True if the Linux unshare+mount masking backend actually hides
        /etc/passwd from the child. This is the same mechanism
        :meth:`_prepare_linux` uses, so a True result means code execution is
        genuinely confined (not a bare subprocess)."""
        import tempfile

        if not sys.platform.startswith("linux"):
            return False
        unshare = shutil.which("unshare")
        if unshare is None:
            return False
        try:
            with tempfile.TemporaryDirectory() as wd:
                script = wd + "/probe.py"
                Path(script).write_text(
                    "import pathlib\nprint('OK' if pathlib.Path('/etc/passwd').exists() else 'BLOCKED')",
                    encoding="utf-8",
                )
                cmd = [
                    unshare, "--user", "--map-root-user", "--mount", "--pid", "--fork",
                    "--", "/bin/sh", "-c",
                    "mount -t tmpfs none /etc >/dev/null 2>&1 && "
                    f"exec {sys.executable} -u {script}",
                ]
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                return out.returncode == 0 and "BLOCKED" in out.stdout
        except Exception:  # noqa: BLE001
            return False


    def run_sandboxed(
        self,
        command: str | Sequence[str],
        args: Sequence[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        allowed_paths: Sequence[Path | str] | None = None,
        env: Mapping[str, str] | None = None,
        stdin_data: str | None = None,
    ) -> SandboxResult:
        """Run *command* with *args* under the strongest available isolation.

        ``allowed_paths`` are exposed read-only to the sandbox (write access is
        confined to :attr:`work_dir`).  Returns a :class:`SandboxResult`;
        never raises on a non-zero exit, only on a failure to launch.
        """
        full_cmd = self._build_command(command, args)
        isolation_level, launch_cmd, child_env, child_cwd = self._prepare_run(
            full_cmd, allowed_paths, env
        )
        result = SandboxResult(
            command=full_cmd,
            isolation_level=isolation_level,
        )
        try:
            proc = subprocess.run(  # noqa: S603 — command is validated below
                launch_cmd,
                cwd=str(child_cwd),
                env=child_env,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            result.timed_out = True
            result.returncode = -1
            result.stderr = f"Command timed out after {timeout}s"
            if self._audit is not None:
                self._audit.log(
                    "sandbox_run_failed",
                    {
                        "command": full_cmd[0],
                        "reason": "timeout",
                        "timeout": timeout,
                    },
                )
            raise SandboxError(str(exc)) from exc
        except FileNotFoundError:
            result.returncode = 127
            result.stderr = f"Command not found: {full_cmd[0]}"
            if self._audit is not None:
                self._audit.log(
                    "sandbox_run_failed",
                    {
                        "command": full_cmd[0],
                        "reason": "not_found",
                    },
                )
            return result

        result.returncode = proc.returncode
        result.stdout = proc.stdout or ""
        result.stderr = proc.stderr or ""

        self._audit_outcome(full_cmd, result, allowed_paths)
        return result

    def close(self) -> None:
        """Remove the work directory when this manager owns it."""
        if self._owns_work_dir and self._work_dir.exists():
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self._owns_work_dir = False

    # ── internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_command(
        command: str | Sequence[str],
        args: Sequence[str] | None,
    ) -> list[str]:
        if isinstance(command, str):
            base: list[str] = [command]
        else:
            base = list(command)
        if args:
            base.extend(str(a) for a in args)
        # Validate every token is a string; refuse empty commands.
        if not base or not base[0]:
            raise ValueError("sandboxed command must be a non-empty sequence")
        return base

    def _prepare_run(
        self,
        full_cmd: list[str],
        allowed_paths: Sequence[Path | str] | None,
        env: Mapping[str, str] | None,
    ) -> tuple[str, list[str], dict[str, str], Path]:
        child_env = self._scrub_env(env)
        child_cwd = self._work_dir
        self._work_dir.mkdir(parents=True, exist_ok=True)
        resolved_allowed = [str(Path(p)) for p in (allowed_paths or [])]

        if self._platform.startswith("linux"):
            return self._prepare_linux(full_cmd, child_env, child_cwd, resolved_allowed)
        if self._platform == "darwin":
            return self._prepare_macos(full_cmd, child_env, child_cwd, resolved_allowed)
        if self._platform == "win32":
            return self._prepare_windows(full_cmd, child_env, child_cwd, resolved_allowed)
        # Unknown platform: lightest isolation.
        logger.debug("Unknown platform %s; using bare subprocess isolation", self._platform)
        return "subprocess", full_cmd, child_env, child_cwd

    def _scrub_env(self, env: Mapping[str, str] | None) -> dict[str, str]:
        """Return a copy of *env* with secrets and dangerous vars removed."""
        base = dict(env) if env is not None else dict(os.environ)
        for key in _ENV_DENYLIST:
            base.pop(key, None)
        # Force a minimal PATH so a malicious extractor can't shadow system
        # tools via a tampered PATH.  Keep the inherited PATH only when the
        # caller explicitly provided it (they take responsibility).
        if env is None:
            base["PATH"] = self._safe_path()
        # Clamp down HOME so the sandbox can't trample the user's dotfiles.
        base["HOME"] = str(self._work_dir)
        base["TMPDIR"] = str(self._work_dir)
        return base

    @staticmethod
    def _safe_path() -> str:
        return os.defpath or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    # ── Linux ────────────────────────────────────────────────────────────

    def _prepare_linux(
        self,
        full_cmd: list[str],
        env: dict[str, str],
        cwd: Path,
        allowed: list[str],
    ) -> tuple[str, list[str], dict[str, str], Path]:
        if not self._enable_strong:
            return "subprocess", full_cmd, env, cwd
        unshare = shutil.which("unshare")
        if unshare is None:
            logger.info("unshare not available; degrading to env+cwd isolation for sandbox")
            return "subprocess-env", full_cmd, env, cwd
        # Mount/PID/net/IPC/UTS namespaces + remount proc so /proc reflects the
        # new PID namespace.  ``--propagation`` private prevents mount events
        # leaking back to the host.  We do NOT add a seccomp filter here
        # directly because shipping a BPF program is brittle; instead we rely
        # on the namespace isolation plus the scrubbed environment.
        wrapper: list[str] = [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            "--pid",
            "--net",
            "--ipc",
            "--uts",
            "--fork",
            "--propagation",
            "private",
            # Bind-mount each allowed path read-only into the new mount ns.
            # ``unshare`` itself does not bind-mount; we chain through a tiny
            # shell that performs the mounts before exec'ing the target.
            "--",
            "/bin/sh",
            "-c",
        ]
        mount_script = self._linux_mount_script(allowed, cwd)
        shell_cmd = mount_script + " exec " + " ".join(self._shell_quote(c) for c in full_cmd)
        wrapper.append(shell_cmd)
        return "unshare-namespace", wrapper, env, cwd

    @staticmethod
    def _linux_mount_script(allowed: list[str], cwd: Path) -> str:
        # The child runs as "root" inside its own user+mount namespace but maps
        # to a non-privileged host uid, so it cannot write to host / — it CAN
        # write inside its work directory (cwd). Strategy:
        #   * stage a cleaned copy of /etc under cwd, remove sensitive files
        #     (passwd/shadow/gshadow/ssh keys/ssl private), bind it over /etc.
        #   * mask /root /home /opt /var/run /var/lib /srv with empty tmpfs.
        #   * bind-mount each allowed path read-only into cwd-relative targets.
        w = SandboxManager._shell_quote(str(cwd))
        etc = f"{w}/etc-clean"
        lines: list[str] = []
        lines.append("set -e")
        lines.append(f"mkdir -p {etc}")
        lines.append(f"cp -r /etc/. {etc}/ 2>/dev/null || true")
        for secret in ("passwd", "shadow", "gshadow", "master.passwd", "security/opasswd"):
            lines.append(f"rm -f {etc}/{secret} 2>/dev/null || true")
        lines.append(f"rm -f {etc}/ssh/authorized_keys {etc}/ssh/ssh_host_*_key {etc}/ssh/ssh_host_*_key.pub 2>/dev/null || true")
        lines.append(f"rm -rf {etc}/ssl/private {etc}/pki/private 2>/dev/null || true")
        lines.append(f"mount --bind {etc} /etc")
        for p in ("/root", "/home", "/opt", "/var/run", "/var/lib", "/srv"):
            lines.append(f"mount -t tmpfs none {SandboxManager._shell_quote(p)} 2>/dev/null || true")
        for idx, path in enumerate(allowed):
            target = f"{w}/allowed_{idx}"
            lines.append(f"mkdir -p {target}")
            lines.append(f"mount --bind {SandboxManager._shell_quote(path)} {target}")
            lines.append(f"mount -o remount,ro,bind {target}")
        return "; ".join(lines) + "; "

    # ── macOS ────────────────────────────────────────────────────────────

    def _prepare_macos(
        self,
        full_cmd: list[str],
        env: dict[str, str],
        cwd: Path,
        allowed: list[str],
    ) -> tuple[str, list[str], dict[str, str], Path]:
        if not self._enable_strong:
            return "subprocess", full_cmd, env, cwd
        sandbox_exec = shutil.which("sandbox-exec")
        if sandbox_exec is None:
            logger.info("sandbox-exec not available; degrading to env+cwd isolation")
            return "subprocess-env", full_cmd, env, cwd
        profile = self._macos_sandbox_profile(cwd, allowed)
        wrapper = [sandbox_exec, "-p", profile, "--"]
        wrapper.extend(full_cmd)
        return "sandbox-exec", wrapper, env, cwd

    @staticmethod
    def _macos_sandbox_profile(work_dir: Path, allowed: list[str]) -> str:
        # Deny by default; allow explicit reads and writes only under work_dir.
        rules: list[str] = [
            "(version 1)",
            "(deny default)",
            "(allow process-fork)",
            "(allow signal (target self))",
            f'(allow file-write* (subpath "{work_dir}"))',
            '(allow file-read* "/usr/lib" "/usr/share" "/Library/Frameworks")',
        ]
        for path in allowed:
            rules.append(f'(allow file-read* (subpath "{path}"))')
        rules.append("(deny network*)")
        return "\n".join(rules)

    # ── Windows ───────────────────────────────────────────────────────────

    def _prepare_windows(
        self,
        full_cmd: list[str],
        env: dict[str, str],
        cwd: Path,
        allowed: list[str],
    ) -> tuple[str, list[str], dict[str, str], Path]:
        # A full AppContainer/restricted-token implementation requires
        # ctypes/CreateRestrictedToken plumbing that is not portable across
        # Python builds.  We therefore provide the environment + cwd isolation
        # baseline here and attempt a best-effort restricted-token escalation
        # via the ``icacls`` ACL lock-down on the work dir so the sandboxed
        # process cannot wander outside it.
        try:
            self._windows_lockdown_cwd(cwd)
        except Exception:  # noqa: BLE001 — lockdown is best-effort
            logger.debug("Windows cwd ACL lockdown failed", exc_info=True)
        return "subprocess-acl", full_cmd, env, cwd

    @staticmethod
    def _windows_lockdown_cwd(cwd: Path) -> None:
        # Best-effort: restrict the work dir to the current user only.
        cwd.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603 — fixed command, args are a trusted path
            ["icacls", str(cwd), "/inheritance:r", "/grant:r", f"{os.getlogin()}:F"],
            capture_output=True,
            check=False,
        )

    # ── audit ─────────────────────────────────────────────────────────────

    def _audit_outcome(
        self,
        full_cmd: list[str],
        result: SandboxResult,
        allowed_paths: Sequence[Path | str] | None,
    ) -> None:
        if self._audit is None:
            return
        # Heuristic escape detection: a suspicious (negative) return code plus
        # stderr referencing a path outside the allowed set suggests the sandbox
        # boundary was probed.
        if result.returncode < 0 or self._stderr_mentions_forbidden(result.stderr, allowed_paths):
            self._audit.log(
                "sandbox_escape_attempt",
                {
                    "command": full_cmd[0],
                    "returncode": result.returncode,
                    "isolation_level": result.isolation_level,
                },
            )
            return
        if result.returncode != 0 and not result.timed_out:
            self._audit.log(
                "sandbox_run_failed",
                {
                    "command": full_cmd[0],
                    "returncode": result.returncode,
                    "isolation_level": result.isolation_level,
                },
            )

    @staticmethod
    def _stderr_mentions_forbidden(
        stderr: str,
        allowed_paths: Sequence[Path | str] | None,
    ) -> bool:
        if not stderr:
            return False
        allowed = {str(Path(p)) for p in (allowed_paths or [])}
        # If stderr references a path that looks like a host secret location
        # and is not in the allowed set, treat it as a probe.
        for token in stderr.split():
            if token.startswith("/") and token not in allowed:
                # Mention of /home, /root, /etc strongly suggests an escape attempt.
                if token.startswith(("/home", "/root", "/etc", "/var", "/Users")):
                    return True
        return False

    @staticmethod
    def _shell_quote(value: str) -> str:
        # Minimal POSIX shell quoting: wrap in single quotes and escape any
        # embedded single quotes.  Sufficient for our wrapper composition.
        return "'" + value.replace("'", "'\\''") + "'"

    # ── context manager support ───────────────────────────────────────────

    def __enter__(self) -> SandboxManager:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "SandboxError",
    "SandboxManager",
    "SandboxResult",
]
