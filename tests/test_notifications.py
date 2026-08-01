"""Tests for cross-platform desktop notifications (plyer-backed)."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from doctoragent.connections.notifications import (
    DesktopNotifier,
    _is_plyer_available,
)


# ── Module-level availability and construction ──────────────────────────────


def test_is_plyer_available_returns_bool() -> None:
    """_is_plyer_available returns a boolean."""
    assert isinstance(_is_plyer_available(), bool)


def test_desktop_notifier_constructs() -> None:
    """DesktopNotifier can be instantiated regardless of plyer presence."""
    notifier = DesktopNotifier()
    assert notifier is not None


# ── notify() delegation ─────────────────────────────────────────────────────


def test_notify_returns_false_when_plyer_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify() returns False when plyer is not available."""
    notifier = DesktopNotifier()
    # Force the plyer facade to None to simulate an unavailable backend.
    notifier._plyer = None
    monkeypatch.setattr(
        "doctoragent.connections.notifications._PLYER_AVAILABLE", False
    )
    assert notifier.notify("Title", "Message") is False


def test_notify_delegates_to_plyer(monkeypatch: pytest.MonkeyPatch) -> None:
    """notify() calls plyer.notification.notify with title and message."""
    calls: list[dict] = []

    class FakePlyer:
        @staticmethod
        def notify(**kwargs: object) -> None:
            calls.append(kwargs)

    notifier = DesktopNotifier()
    notifier._plyer = FakePlyer  # type: ignore[assignment]
    monkeypatch.setattr(
        "doctoragent.connections.notifications._PLYER_AVAILABLE", True
    )

    result = notifier.notify("Hello", "World")
    assert result is True
    assert len(calls) == 1
    assert calls[0]["title"] == "Hello"
    assert calls[0]["message"] == "World"
    assert calls[0]["app_name"] == "DoctorAgent"


def test_notify_maps_critical_urgency_to_longer_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical urgency maps to a longer timeout value."""
    calls: list[dict] = []

    class FakePlyer:
        @staticmethod
        def notify(**kwargs: object) -> None:
            calls.append(kwargs)

    notifier = DesktopNotifier()
    notifier._plyer = FakePlyer  # type: ignore[assignment]
    monkeypatch.setattr(
        "doctoragent.connections.notifications._PLYER_AVAILABLE", True
    )

    notifier.notify("Title", "Msg", urgency="critical")
    assert calls[0]["timeout"] == 10

    notifier.notify("Title", "Msg", urgency="normal")
    assert calls[1]["timeout"] == 5


def test_notify_returns_false_on_plyer_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify() returns False when plyer raises an exception."""

    class FakePlyer:
        @staticmethod
        def notify(**kwargs: object) -> None:
            raise OSError("notification daemon unavailable")

    notifier = DesktopNotifier()
    notifier._plyer = FakePlyer  # type: ignore[assignment]
    monkeypatch.setattr(
        "doctoragent.connections.notifications._PLYER_AVAILABLE", True
    )

    assert notifier.notify("Title", "Msg") is False


# ── Convenience methods ─────────────────────────────────────────────────────


def test_notify_classification_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """notify_classification_done delegates to notify with correct params."""
    calls: list[tuple] = []

    notifier = DesktopNotifier()

    # Monkeypatch notify to capture arguments.
    def fake_notify(title: str, message: str, urgency: str = "normal") -> bool:
        calls.append((title, message, urgency))
        return True

    monkeypatch.setattr(notifier, "notify", fake_notify)

    assert notifier.notify_classification_done("report.pdf") is True
    assert len(calls) == 1
    assert "report.pdf" in calls[0][1]
    assert calls[0][2] == "low"
    assert "Classified" in calls[0][0]


def test_notify_security_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    """notify_security_alert delegates to notify with critical urgency."""
    calls: list[tuple] = []

    notifier = DesktopNotifier()

    def fake_notify(title: str, message: str, urgency: str = "normal") -> bool:
        calls.append((title, message, urgency))
        return True

    monkeypatch.setattr(notifier, "notify", fake_notify)

    assert (
        notifier.notify_security_alert("unauthorized_access", "Someone tried to open vault")
        is True
    )
    assert len(calls) == 1
    assert "unauthorized_access" in calls[0][0]
    assert "Someone tried to open vault" in calls[0][1]
    assert calls[0][2] == "critical"


def test_notify_sync_complete_removed() -> None:
    """notify_sync_complete was removed (dead code cleanup)."""
    notifier = DesktopNotifier()
    assert not hasattr(notifier, "notify_sync_complete")


# ── Agent integration ───────────────────────────────────────────────────────


def test_agent_receives_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """AegisAgent accepts an optional notifier parameter."""
    from doctoragent.config import AegisConfig
    from doctoragent.orchestration.agent import AegisAgent

    config = AegisConfig()
    config.paths.index.mkdir(parents=True, exist_ok=True)

    mock_key = MagicMock()
    mock_key.get_key.return_value = b"\x00" * 32
    mock_classifier = MagicMock()

    mock_notifier = MagicMock()
    agent = AegisAgent(
        config,
        notifier=mock_notifier,
        master_key_provider=mock_key,
        classifier=mock_classifier,
    )
    assert agent._notifier is mock_notifier


def test_agent_sends_classification_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent notifies on successful file classification."""
    from uuid import uuid4

    from doctoragent.api.schemas import FileEvent, TaskStatus
    from doctoragent.config import AegisConfig
    from doctoragent.orchestration.agent import AegisAgent

    config = AegisConfig()
    config.paths.inbox = tmp_path / "Inbox"
    config.paths.vault = tmp_path / "Vault"
    config.paths.index = tmp_path / "Index"
    for p in [config.paths.inbox, config.paths.vault, config.paths.index]:
        p.mkdir(parents=True, exist_ok=True)

    notifier = MagicMock()

    mock_key = MagicMock()
    mock_key.get_key.return_value = b"\x00" * 32
    mock_classifier = MagicMock()

    agent = AegisAgent(
        config,
        notifier=notifier,
        master_key_provider=mock_key,
        classifier=mock_classifier,
    )

    async def fake_process(event: FileEvent) -> TaskStatus:
        return TaskStatus(task_id=event.event_id, state="completed")

    monkeypatch.setattr(agent, "on_file_event", fake_process)

    event = FileEvent(
        event_id=uuid4(),
        source_path=Path("/tmp/test.pdf"),
    )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(agent._handle_event(event))
    finally:
        loop.close()

    notifier.notify_classification_done.assert_called_once_with("test.pdf")


def test_agent_sends_failure_notification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent sends security alert notification on processing failure."""
    from uuid import uuid4

    from doctoragent.api.schemas import FileEvent
    from doctoragent.config import AegisConfig
    from doctoragent.orchestration.agent import AegisAgent

    config = AegisConfig()
    config.paths.inbox = tmp_path / "Inbox"
    config.paths.vault = tmp_path / "Vault"
    config.paths.index = tmp_path / "Index"
    for p in [config.paths.inbox, config.paths.vault, config.paths.index]:
        p.mkdir(parents=True, exist_ok=True)

    notifier = MagicMock()

    mock_key = MagicMock()
    mock_key.get_key.return_value = b"\x00" * 32
    mock_classifier = MagicMock()

    agent = AegisAgent(
        config,
        notifier=notifier,
        master_key_provider=mock_key,
        classifier=mock_classifier,
    )

    async def fake_process(event: FileEvent) -> None:
        raise ValueError("simulated failure")

    monkeypatch.setattr(agent, "on_file_event", fake_process)

    event = FileEvent(
        event_id=uuid4(),
        source_path=Path("/tmp/bad_file.txt"),
    )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(agent._handle_event(event))
    finally:
        loop.close()

    notifier.notify_security_alert.assert_called_once()
    call_args = notifier.notify_security_alert.call_args
    assert call_args[0][0] == "processing_failure"
    assert "bad_file.txt" in call_args[0][1]
