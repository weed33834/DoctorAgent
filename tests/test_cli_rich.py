# mypy: ignore-errors
"""Tests for Phase 7.4 CLI rich features.

Covers: batch import, JSON orchestration scripts, shell completion via
click's built-in completion, and shared helper functions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from doctoragent.__main__ import (
    _resolve_import_targets,
    _stage_into_inbox,
    cmd_import,
    cmd_run,
)
from doctoragent.api.schemas import TaskStatus
from doctoragent.config import AegisConfig

# ── Helpers ─────────────────────────────────────────────────────────────────


def _config(tmp_path: Path) -> AegisConfig:
    """Build a config with inbox/vault rooted under *tmp_path*."""
    cfg = AegisConfig()
    cfg.paths.inbox = tmp_path / "Inbox"
    cfg.paths.vault = tmp_path / "Vault"
    cfg.paths.index = tmp_path / "Index"
    return cfg


def _completed_status() -> TaskStatus:
    return TaskStatus(task_id=uuid4(), state="COMPLETED")


def _failed_status() -> TaskStatus:
    return TaskStatus(task_id=uuid4(), state="FAILED", message="boom")


def _mock_agent(status: TaskStatus | None = None) -> MagicMock:
    """Build a mock AegisAgent whose on_file_event returns *status*."""
    agent = MagicMock()
    agent.audit_logger = None
    agent.on_file_event = AsyncMock(return_value=status or _completed_status())
    return agent


# ── _resolve_import_targets ─────────────────────────────────────────────────


def test_resolve_targets_single_files(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("a")
    f2.write_text("b")
    targets = _resolve_import_targets([f1, f2], is_dir=False)
    assert targets == [f1, f2]


def test_resolve_targets_directory_recursive(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    f1 = tmp_path / "top.txt"
    f2 = sub / "nested.txt"
    f1.write_text("1")
    f2.write_text("2")
    targets = _resolve_import_targets([tmp_path], is_dir=True)
    assert f1 in targets
    assert f2 in targets


def test_resolve_targets_skips_nonexistent(tmp_path: Path) -> None:
    targets = _resolve_import_targets([tmp_path / "nope.txt"], is_dir=False)
    assert targets == []


@pytest.mark.skipif(sys.platform == "win32", reason="Symlink creation requires elevated privileges on Windows")
def test_resolve_targets_skips_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    targets = _resolve_import_targets([link], is_dir=False)
    assert targets == []


# ── _stage_into_inbox ───────────────────────────────────────────────────────


def test_stage_copy_preserves_original(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("hello")
    inbox = tmp_path / "inbox"
    dest = _stage_into_inbox(src, inbox, move=False)
    assert dest.read_text() == "hello"
    assert src.exists(), "copy should preserve the original"


def test_stage_move_removes_original(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("hello")
    inbox = tmp_path / "inbox"
    dest = _stage_into_inbox(src, inbox, move=True)
    assert dest.read_text() == "hello"
    assert not src.exists(), "move should consume the original"


def test_stage_collision_safe(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("first")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "src.txt").write_text("existing")
    dest = _stage_into_inbox(src, inbox, move=False)
    assert dest.name != "src.txt"
    assert dest.read_text() == "first"
    assert (inbox / "src.txt").read_text() == "existing"


# ── cmd_import ──────────────────────────────────────────────────────────────


def test_import_no_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _config(tmp_path)
    agent = _mock_agent()
    rc = cmd_import(agent, cfg, [], is_dir=False, move=False, no_wait=False)
    assert rc == 1


def test_import_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("content a")
    f2.write_text("content b")
    cfg = _config(tmp_path)
    agent = _mock_agent(_completed_status())
    rc = cmd_import(agent, cfg, [f1, f2], is_dir=False, move=False, no_wait=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Imported 2/2" in out
    # Originals preserved (copy mode).
    assert f1.exists()
    assert f2.exists()


def test_import_move_consumes_originals(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f1.write_text("content")
    cfg = _config(tmp_path)
    agent = _mock_agent(_completed_status())
    cmd_import(agent, cfg, [f1], is_dir=False, move=True, no_wait=False)
    assert not f1.exists(), "move should consume the original"


def test_import_failure_returns_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f1 = tmp_path / "a.txt"
    f1.write_text("content")
    cfg = _config(tmp_path)
    agent = _mock_agent(_failed_status())
    rc = cmd_import(agent, cfg, [f1], is_dir=False, move=False, no_wait=False)
    assert rc == 1


def test_import_no_wait_stages_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f1 = tmp_path / "a.txt"
    f1.write_text("content")
    cfg = _config(tmp_path)
    agent = _mock_agent()
    # on_file_event should never be called in no-wait mode.
    rc = cmd_import(agent, cfg, [f1], is_dir=False, move=False, no_wait=True)
    assert rc == 0
    agent.on_file_event.assert_not_called()
    out = capsys.readouterr().out
    assert "Staged" in out
    # The staged file should exist in inbox.
    staged = list(cfg.paths.inbox.iterdir())
    assert len(staged) == 1


def test_import_directory_mode(tmp_path: Path) -> None:
    src_dir = tmp_path / "srcs"
    src_dir.mkdir()
    (src_dir / "a.txt").write_text("a")
    (src_dir / "b.txt").write_text("b")
    cfg = _config(tmp_path)
    agent = _mock_agent(_completed_status())
    rc = cmd_import(agent, cfg, [src_dir], is_dir=True, move=False, no_wait=False)
    assert rc == 0


# ── cmd_run (orchestration) ─────────────────────────────────────────────────


def test_run_dry_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"steps": [{"op": "status"}, {"op": "list", "category": "work"}]}))
    cfg = _config(tmp_path)
    agent = _mock_agent()
    rc = cmd_run(agent, cfg, script, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 step(s)" in out
    assert "status" in out
    assert "list" in out


def test_run_invalid_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = tmp_path / "bad.json"
    script.write_text("{not valid json")
    cfg = _config(tmp_path)
    agent = _mock_agent()
    rc = cmd_run(agent, cfg, script, dry_run=False)
    assert rc == 1


def test_run_missing_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _config(tmp_path)
    agent = _mock_agent()
    rc = cmd_run(agent, cfg, tmp_path / "nope.json", dry_run=False)
    assert rc == 1


def test_run_empty_steps(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"steps": []}))
    cfg = _config(tmp_path)
    agent = _mock_agent()
    rc = cmd_run(agent, cfg, script, dry_run=False)
    assert rc == 1


def test_run_unknown_op_aborts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"steps": [{"op": "frobnicate"}]}))
    cfg = _config(tmp_path)
    agent = _mock_agent()
    rc = cmd_run(agent, cfg, script, dry_run=False)
    assert rc == 1


def test_run_executes_status_step(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"steps": [{"op": "status"}]}))
    cfg = _config(tmp_path)
    agent = _mock_agent()
    # cmd_status calls agent.task_store.list_recent — provide a mock.
    agent.task_store.list_recent.return_value = []
    rc = cmd_run(agent, cfg, script, dry_run=False)
    assert rc == 0


def test_run_from_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path)
    agent = _mock_agent()
    agent.task_store.list_recent.return_value = []
    script_text = json.dumps({"steps": [{"op": "status"}]})
    monkeypatch.setattr("sys.stdin", SimpleNamespace(read=lambda: script_text))
    rc = cmd_run(agent, cfg, Path("-"), dry_run=False)
    assert rc == 0


# ── Integration: click completion command ──────────────────────────────────


def test_main_completion_dispatches(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main(['completion', '--shell', 'fish']) emits a fish completion script."""
    from doctoragent.__main__ import main

    rc = main(["completion", "--shell", "fish"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "doctoragent" in out
