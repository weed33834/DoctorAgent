"""Tests: container sandbox backend (v0.3.20).

Adds an opt-in Docker/Podman isolation layer on top of the platform
backends (``DOCTORAGENT_SANDBOX_CONTAINER=1`` or ``container_backend=True``).
All engine detection and subprocess execution are mocked — no real Docker
required.

Hardening contract of the generated argv:

* ``--network none``            → no outbound/inbound traffic
* ``--cpus=1 --memory=256m``    → compute caps
* ``--pids-limit=128``          → fork-bomb cap
* ``--read-only`` + ``--tmpfs /tmp``  → immutable rootfs, writable scratch
* ``--security-opt no-new-privileges`` → no privilege escalation
* work dir bind-mounted at /sandbox; allowed_paths read-only
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from doctoragent.security.sandbox import SandboxManager


@pytest.fixture(autouse=True)
def _no_container_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DOCTORAGENT_SANDBOX_CONTAINER", raising=False)


def _engine_ok(monkeypatch: pytest.MonkeyPatch, exe: str = "/usr/bin/docker") -> None:
    monkeypatch.setattr(
        "doctoragent.security.sandbox.shutil.which", lambda name: exe if name in ("docker", "podman") else None
    )
    monkeypatch.setattr(
        "doctoragent.security.sandbox.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="24.0.7"),
    )


class TestContainerBackend:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _engine_ok(monkeypatch)
        m = SandboxManager(enable_strong_isolation=True)
        assert m._container_enabled is False
        level, cmd, _env, _cwd = m._prepare_run(["python", "-c", "x"], [], None)
        assert level != "container"

    @pytest.mark.parametrize("kwarg,env", [(True, None), (None, "1")])
    def test_enabled_via_kwarg_or_env(
        self, monkeypatch: pytest.MonkeyPatch, kwarg: bool | None, env: str | None
    ) -> None:
        if env is not None:
            monkeypatch.setenv("DOCTORAGENT_SANDBOX_CONTAINER", env)
        _engine_ok(monkeypatch)
        m = SandboxManager(container_backend=kwarg)
        assert m._container_enabled is True

    def test_launch_argv_hardening_contract(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _engine_ok(monkeypatch)
        m = SandboxManager(
            container_backend=True,
            work_dir=tmp_path / "work",
            container_image="python:3.12-slim",
        )
        allowed = [tmp_path / "vault"]
        level, cmd = m._container_launch(["python", "-c", "print(1)"], [str(allowed[0])])
        assert level == "container"
        assert cmd[0] == "/usr/bin/docker"
        assert cmd[1] == "run"
        for flag in ("--network", "--rm", "--read-only"):
            assert flag in cmd
        assert cmd[cmd.index("--network") + 1] == "none"
        assert "--cpus=1" in cmd and "--memory=256m" in cmd and "--pids-limit=128" in cmd
        assert cmd[cmd.index("--security-opt") + 1] == "no-new-privileges"
        # work dir mounted at /sandbox, allowed path mounted read-only.
        ro_mounts = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]
        assert f"{allowed[0]}:{allowed[0]}:ro" in ro_mounts
        assert any(s.endswith(":/sandbox") for s in ro_mounts)
        # Image is the first non-flag token after all options.
        assert "python:3.12-slim" in cmd
        assert cmd[-3:] == ["python", "-c", "print(1)"]

    def test_run_sandboxed_uses_container_level(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _engine_ok(monkeypatch)

        captured: dict[str, Any] = {}

        def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="hello", stderr="")

        monkeypatch.setattr("doctoragent.security.sandbox.subprocess.run", fake_run)
        m = SandboxManager(container_backend=True, work_dir=tmp_path / "w")
        result = m.run_sandboxed("python", ["-c", "print('hello')"])
        assert result.isolation_level == "container"
        assert result.ok is True
        assert result.stdout == "hello"
        assert captured["cmd"][0] == "/usr/bin/docker"

    def test_engine_missing_falls_back_to_platform(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "doctoragent.security.sandbox.shutil.which", lambda name: None
        )
        m = SandboxManager(
            container_backend=True, work_dir=tmp_path / "w", enable_strong_isolation=False
        )
        level, cmd, _env, _cwd = m._prepare_run(["python", "-c", "x"], [], None)
        assert level != "container"

    def test_broken_daemon_falls_back(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Engine binary exists but daemon is down → platform fallback."""
        monkeypatch.setattr(
            "doctoragent.security.sandbox.shutil.which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        monkeypatch.setattr(
            "doctoragent.security.sandbox.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="", stderr="Cannot connect to the Docker daemon"),
        )
        m = SandboxManager(
            container_backend=True, work_dir=tmp_path / "w", enable_strong_isolation=False
        )
        assert m._container_engine() is None
        level, cmd, _env, _cwd = m._prepare_run(["python", "-c", "x"], [], None)
        assert level != "container"

    def test_engine_probe_cached_once(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _engine_ok(monkeypatch)
        probe_calls = {"n": 0}

        def counting_run(*a: Any, **k: Any) -> subprocess.CompletedProcess:
            probe_calls["n"] += 1
            return subprocess.CompletedProcess(a[0], 0, stdout="24.0.7")

        monkeypatch.setattr("doctoragent.security.sandbox.subprocess.run", counting_run)
        m = SandboxManager(container_backend=True, work_dir=tmp_path / "w")
        m._container_launch(["python"], [])
        m._container_launch(["python"], [])
        assert probe_calls["n"] == 1  # engine probed once per manager

    def test_isolation_effective_true_with_container(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CodeExecTool's gate accepts a working container engine."""
        monkeypatch.setenv("DOCTORAGENT_SANDBOX_CONTAINER", "1")
        _engine_ok(monkeypatch)
        assert SandboxManager.isolation_effective() is True

    def test_isolation_effective_false_when_container_env_but_no_engine(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """Non-Linux host without engines must NOT pass the gate."""
        import types

        monkeypatch.setenv("DOCTORAGENT_SANDBOX_CONTAINER", "1")
        monkeypatch.setattr(
            "doctoragent.security.sandbox.shutil.which", lambda name: None
        )
        # Simulate non-Linux so the unshare branch also refuses.
        monkeypatch.setattr(
            "doctoragent.security.sandbox.sys", types.SimpleNamespace(platform="win32")
        )
        assert SandboxManager.isolation_effective() is False


class TestCodeExecGateWithContainer:
    def test_code_exec_gate_accepts_container_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DOCTORAGENT_ALLOW_UNSAFE_CODE", raising=False)
        monkeypatch.setenv("DOCTORAGENT_SANDBOX_CONTAINER", "1")
        _engine_ok(monkeypatch)
        from doctoragent.tools.code_exec_tool import CodeExecTool

        assert CodeExecTool._isolation_ok() is True
