"""Tests for the Inbox watchdog-based file watcher."""

import sys
import time
from pathlib import Path
from uuid import UUID

import pytest
from watchdog.events import DirCreatedEvent, FileCreatedEvent

from doctoragent.api.schemas import FileEvent
from doctoragent.execution.inbox_watcher import InboxEventHandler, InboxWatcher


def test_event_handler_invokes_callback_for_files() -> None:
    """File creation events produce FileEvent callbacks."""
    received: list[FileEvent] = []

    def callback(event: FileEvent) -> None:
        received.append(event)

    handler = InboxEventHandler(callback)
    handler.on_created(FileCreatedEvent(src_path="/tmp/Inbox/report.txt"))

    assert len(received) == 1
    assert received[0].event_type == "created"
    assert received[0].source_path == Path("/tmp/Inbox/report.txt")
    assert isinstance(received[0].event_id, UUID)


def test_event_handler_ignores_directories() -> None:
    """Directory creation events do not trigger the callback."""
    received: list[FileEvent] = []

    def callback(event: FileEvent) -> None:
        received.append(event)

    handler = InboxEventHandler(callback)
    handler.on_created(DirCreatedEvent(src_path="/tmp/Inbox/nested"))

    assert len(received) == 0


def test_watcher_lifecycle(tmp_path: Path) -> None:
    """InboxWatcher creates the inbox directory and starts/stops cleanly."""
    inbox = tmp_path / "Inbox"
    watcher = InboxWatcher(inbox, lambda event: None)
    assert not inbox.exists()

    watcher.start()
    try:
        assert inbox.exists()
        assert watcher.observer.is_alive()
    finally:
        watcher.stop()

    assert watcher.observer.is_alive() is False


@pytest.mark.slow
@pytest.mark.skipif(sys.platform == "win32", reason="timing-sensitive on Windows")
def test_watcher_detects_new_file(tmp_path: Path) -> None:
    """InboxWatcher detects a newly created file."""
    inbox = tmp_path / "Inbox"
    inbox.mkdir()
    received: list[FileEvent] = []

    def callback(event: FileEvent) -> None:
        received.append(event)

    watcher = InboxWatcher(inbox, callback)
    watcher.start()
    try:
        (inbox / "hello.txt").write_text("hello")
        # Wait briefly for the observer thread to pick up the event.
        for _ in range(50):
            if received:
                break
            time.sleep(0.05)
    finally:
        watcher.stop()

    assert len(received) == 1
    assert received[0].source_path == inbox / "hello.txt"
