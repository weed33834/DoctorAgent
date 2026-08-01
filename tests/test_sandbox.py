"""Tests for the sandboxed execution manager.

The security-critical paths under test:

1. **``_scrub_env``** — denylist stripping (master key password, LD_PRELOAD,
   PYTHONPATH must never leak into the child) and HOME/TMPDIR/PATH clamping.
   A regression here could expose vault secrets to an untrusted extractor.
2. **``_shell_quote``** — POSIX single-quote escaping. A bug here could
   enable shell injection in the ``unshare`` wrapper script.
3. **``_build_command``** — command validation (no empty commands).
4. **``run_sandboxed``** — happy path, non-zero exit, not-found, timeout.
5. **``_audit_outcome`` / ``_stderr_mentions_forbidden``** — escape-detection
   heuristic and audit event routing.
6. **``SandboxResult.ok``** — the success predicate.
7. **``close`` / context manager** — work-dir cleanup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from doctoragent.security.sandbox import (
    DEFAULT_TIMEOUT_SECONDS,
    SandboxError,
    SandboxManager,
    SandboxResult,
    _ENV_DENYLIST,
)

# ---------------------------------------------------------------------------
# SandboxResult.ok
# ---------------------------------------------------------------------------


class TestSandboxResultOk:
    def test_ok_zero_exit_no_timeout(self) -> None:
        assert SandboxResult(returncode=0).ok is True

    def test_not_ok_nonzero_exit(self) -> None:
        assert SandboxResult(returncode=1).ok is False

    def test_not_ok_timed_out(self) -> None:
        assert SandboxResult(returncode=0, timed_out=True).ok is False

    def test_not_ok_negative_returncode(self) -> None:
        assert SandboxResult(returncode=-1).ok is False


# ---------------------------------------------------------------------------
# _build_command
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_string_command(self) -> None:
        cmd = SandboxManager._build_command("echo", ["hello", "world"])
        assert cmd == ["echo", "hello", "world"]

    def test_sequence_command(self) -> None:
        cmd = SandboxManager._build_command(["python3", "-c"], ["print(1)"])
        assert cmd == ["python3", "-c", "print(1)"]

    def test_no_args(self) -> None:
        cmd = SandboxManager._build_command("echo", None)
        assert cmd == ["echo"]

    def test_empty_string_command_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            SandboxManager._build_command("", [])

    def test_empty_sequence_command_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            SandboxManager._build_command([], None)

    def test_args_coerced_to_str(self) -> None:
        cmd = SandboxManager._build_command("echo", [42, True])
        assert cmd == ["echo", "42", "True"]


# ---------------------------------------------------------------------------
# _scrub_env (SECURITY CRITICAL)
# ---------------------------------------------------------------------------


class TestScrubEnv:
    """If these tests fail, vault secrets could leak into the sandbox."""

    def test_strips_denylisted_secrets(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        env = {
            "DOCTORAGENT_MASTER_KEY_PASSWORD": "super-secret",
            "DOCTORAGENT_SECURITY__MASTER_KEY_PASSWORD": "super-secret",
            "PYTHONPATH": "/evil/path",
            "LD_PRELOAD": "/evil/lib.so",
            "LD_LIBRARY_PATH": "/evil/lib",
            "DYLD_INSERT_LIBRARIES": "/evil/dylib",
            "PATH": "/usr/bin",
            "HOME": "/home/user",
        }
        scrubbed = mgr._scrub_env(env)
        for key in _ENV_DENYLIST:
            assert key not in scrubbed, f"denylisted {key} leaked into sandbox env"

    def test_clamps_home_and_tmpdir_to_work_dir(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        scrubbed = mgr._scrub_env({"PATH": "/usr/bin"})
        assert scrubbed["HOME"] == str(mgr.work_dir)
        assert scrubbed["TMPDIR"] == str(mgr.work_dir)
        mgr.close()

    def test_forces_safe_path_when_env_not_provided(self) -> None:
        # When env is None (inheriting os.environ), PATH is forced to a
        # minimal safe value to prevent PATH hijacking.
        mgr = SandboxManager(enable_strong_isolation=False)
        scrubbed = mgr._scrub_env(None)
        assert scrubbed["PATH"] == mgr._safe_path()
        mgr.close()

    def test_preserves_caller_path_when_env_provided(self) -> None:
        # When the caller explicitly provides env, they take responsibility
        # for PATH — we don't override it.
        mgr = SandboxManager(enable_strong_isolation=False)
        scrubbed = mgr._scrub_env({"PATH": "/custom/bin", "HOME": "/tmp"})
        assert scrubbed["PATH"] == "/custom/bin"
        mgr.close()

    def test_does_not_mutate_input_env(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        env = {"DOCTORAGENT_MASTER_KEY_PASSWORD": "secret", "PATH": "/usr/bin"}
        original = dict(env)
        mgr._scrub_env(env)
        assert env == original  # input dict not mutated
        mgr.close()

    def test_all_denylist_entries_are_stripped(self) -> None:
        # Exhaustive: every entry in _ENV_DENYLIST must be stripped.
        mgr = SandboxManager(enable_strong_isolation=False)
        env = {key: "leak" for key in _ENV_DENYLIST}
        env["PATH"] = "/usr/bin"
        scrubbed = mgr._scrub_env(env)
        for key in _ENV_DENYLIST:
            assert key not in scrubbed
        mgr.close()


# ---------------------------------------------------------------------------
# _shell_quote (SECURITY CRITICAL — prevents injection in wrapper script)
# ---------------------------------------------------------------------------


class TestShellQuote:
    def test_simple_string(self) -> None:
        assert SandboxManager._shell_quote("echo") == "'echo'"

    def test_string_with_spaces(self) -> None:
        assert SandboxManager._shell_quote("hello world") == "'hello world'"

    def test_string_with_single_quote(self) -> None:
        # Embedded single quote is escaped as '\'' (close, escaped quote, reopen).
        result = SandboxManager._shell_quote("it's")
        # The result must, when used in a shell, expand to: it's
        # Standard escaping: 'it'\''s'
        assert result == "'it'\\''s'"

    def test_string_with_special_chars(self) -> None:
        result = SandboxManager._shell_quote("$(rm -rf /)")
        assert result == "'$(rm -rf /)'"
        # Single-quoted, so the $() is NOT executed by the shell.
        assert "$(" in result

    def test_empty_string(self) -> None:
        assert SandboxManager._shell_quote("") == "''"

    def test_injection_attempt_is_neutralized(self) -> None:
        # A malicious filename attempting shell command substitution.
        malicious = "file; rm -rf /"
        result = SandboxManager._shell_quote(malicious)
        # When embedded in a shell command, the semicolon is inside single
        # quotes and thus treated as a literal character, not a separator.
        assert ";" in result
        assert result == "'file; rm -rf /'"


# ---------------------------------------------------------------------------
# _stderr_mentions_forbidden
# ---------------------------------------------------------------------------


class TestStderrMentionsForbidden:
    def test_clean_stderr_returns_false(self) -> None:
        assert SandboxManager._stderr_mentions_forbidden("normal output", None) is False

    def test_empty_stderr_returns_false(self) -> None:
        assert SandboxManager._stderr_mentions_forbidden("", None) is False

    def test_home_path_detected(self) -> None:
        stderr = "error: cannot access /home/user/.config/doctoragent"
        assert SandboxManager._stderr_mentions_forbidden(stderr, None) is True

    def test_root_path_detected(self) -> None:
        assert SandboxManager._stderr_mentions_forbidden("open: /root/.bashrc", None) is True

    def test_etc_path_detected(self) -> None:
        assert SandboxManager._stderr_mentions_forbidden("cat /etc/passwd", None) is True

    def test_allowed_path_not_flagged(self) -> None:
        stderr = "reading /sandbox/allowed_0/data"
        assert SandboxManager._stderr_mentions_forbidden(stderr, ["/sandbox/allowed_0"]) is False

    def test_non_host_path_not_flagged(self) -> None:
        # A path that doesn't start with /home, /root, /etc, /var, /Users.
        assert SandboxManager._stderr_mentions_forbidden("/tmp/random", None) is False


# ---------------------------------------------------------------------------
# _audit_outcome
# ---------------------------------------------------------------------------


class TestAuditOutcome:
    def test_no_audit_when_logger_none(self) -> None:
        mgr = SandboxManager(audit_logger=None, enable_strong_isolation=False)
        # Should not raise even though there's no logger.
        mgr._audit_outcome(["cmd"], SandboxResult(returncode=1), None)
        mgr.close()

    def test_nonzero_exit_logs_run_failed(self) -> None:
        audit = MagicMock()
        mgr = SandboxManager(audit_logger=audit, enable_strong_isolation=False)
        mgr._audit_outcome(["cmd"], SandboxResult(returncode=2), None)
        audit.log.assert_called_once_with(
            "sandbox_run_failed",
            {"command": "cmd", "returncode": 2, "isolation_level": "subprocess"},
        )
        mgr.close()

    def test_escape_attempt_logged_on_forbidden_stderr(self) -> None:
        audit = MagicMock()
        mgr = SandboxManager(audit_logger=audit, enable_strong_isolation=False)
        result = SandboxResult(returncode=1, stderr="error: /root/.bashrc")
        mgr._audit_outcome(["cmd"], result, None)
        audit.log.assert_called_once_with(
            "sandbox_escape_attempt",
            {"command": "cmd", "returncode": 1, "isolation_level": "subprocess"},
        )
        mgr.close()

    def test_success_not_logged(self) -> None:
        audit = MagicMock()
        mgr = SandboxManager(audit_logger=audit, enable_strong_isolation=False)
        mgr._audit_outcome(["cmd"], SandboxResult(returncode=0), None)
        audit.log.assert_not_called()
        mgr.close()


# ---------------------------------------------------------------------------
# run_sandboxed (integration — uses real subprocess but simple commands)
# ---------------------------------------------------------------------------


class TestRunSandboxed:
    def test_echo_success(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        try:
            # Use sys.executable instead of "echo" (not a .exe on Windows)
            result = mgr.run_sandboxed(
                sys.executable, ["-c", "print('hello')"]
            )
            assert result.ok is True
            assert "hello" in result.stdout
            assert result.returncode == 0
        finally:
            mgr.close()

    def test_nonzero_exit_returned_not_raised(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        try:
            result = mgr.run_sandboxed(
                sys.executable, ["-c", "import sys; sys.exit(42)"]
            )
            assert result.ok is False
            assert result.returncode == 42
        finally:
            mgr.close()

    def test_command_not_found_returns_127(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        try:
            result = mgr.run_sandboxed("this-command-does-not-exist-xyz")
            assert result.returncode == 127
            assert "not found" in result.stderr.lower()
            assert result.ok is False
        finally:
            mgr.close()

    def test_timeout_raises_sandbox_error(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        try:
            with pytest.raises(SandboxError, match="timed out"):
                mgr.run_sandboxed(
                    sys.executable,
                    ["-c", "import time; time.sleep(100)"],
                    timeout=1.0,
                )
        finally:
            mgr.close()

    def test_stdin_data_passed(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        try:
            result = mgr.run_sandboxed(
                sys.executable,
                ["-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                stdin_data="piped-input",
            )
            assert result.ok is True
            assert "piped-input" in result.stdout
        finally:
            mgr.close()

    def test_work_dir_is_cwd(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        try:
            result = mgr.run_sandboxed(
                sys.executable, ["-c", "import os; print(os.getcwd())"]
            )
            assert result.ok is True
            assert str(mgr.work_dir) in result.stdout
        finally:
            mgr.close()

    def test_default_timeout_is_30s(self) -> None:
        assert DEFAULT_TIMEOUT_SECONDS == 30.0

    def test_isolation_level_reported(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        try:
            # Use sys.executable instead of "echo" (not a .exe on Windows)
            result = mgr.run_sandboxed(
                sys.executable, ["-c", "print('test')"]
            )
            # With strong isolation disabled, expected level varies by platform:
            # Linux/macOS → "subprocess", Windows → "subprocess-acl"
            expected = "subprocess-acl" if sys.platform == "win32" else "subprocess"
            assert result.isolation_level == expected
        finally:
            mgr.close()


# ---------------------------------------------------------------------------
# close / context manager
# ---------------------------------------------------------------------------


class TestCloseAndContextManager:
    def test_close_removes_owned_work_dir(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        work_dir = mgr.work_dir
        assert work_dir.exists()
        mgr.close()
        assert not work_dir.exists()

    def test_close_does_not_remove_external_work_dir(self, tmp_path: Path) -> None:
        mgr = SandboxManager(work_dir=tmp_path, enable_strong_isolation=False)
        mgr.close()
        # External work dir is NOT owned → not removed.
        assert tmp_path.exists()

    def test_context_manager_closes_on_exit(self) -> None:
        with SandboxManager(enable_strong_isolation=False) as mgr:
            work_dir = mgr.work_dir
            assert work_dir.exists()
        assert not work_dir.exists()

    def test_double_close_safe(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        mgr.close()
        mgr.close()  # no error


# ---------------------------------------------------------------------------
# Platform preparation (degradation paths)
# ---------------------------------------------------------------------------


class TestPlatformPreparation:
    def test_strong_isolation_disabled_returns_subprocess(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        level, cmd, env, cwd = mgr._prepare_run(["echo"], None, None)
        expected = "subprocess-acl" if sys.platform == "win32" else "subprocess"
        assert level == expected
        assert cmd == ["echo"]
        mgr.close()

    def test_prepare_run_sets_work_dir_as_cwd(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        _, _, _, cwd = mgr._prepare_run(["echo"], None, None)
        assert cwd == mgr.work_dir
        mgr.close()

    def test_prepare_run_scrubs_env(self) -> None:
        mgr = SandboxManager(enable_strong_isolation=False)
        _, _, env, _ = mgr._prepare_run(["echo"], None, {"DOCTORAGENT_MASTER_KEY_PASSWORD": "leak"})
        assert "DOCTORAGENT_MASTER_KEY_PASSWORD" not in env
        mgr.close()
