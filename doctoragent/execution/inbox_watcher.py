"""Watch Inbox directory for new files."""

import logging
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from watchdog.events import FileCreatedEvent, FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from doctoragent.api.schemas import FileEvent

logger = logging.getLogger(__name__)

# Temporary file patterns that should be ignored by the watcher.
_TEMP_SUFFIXES = (".tmp", ".part", ".crdownload", ".download", ".swp", ".bak")
_TEMP_PREFIXES = ("~", "~$", ".~")


def _is_temp_file(path: Path) -> bool:
    """Return True when *path* looks like a temporary/partial file."""
    name = path.name
    if any(name.startswith(prefix) for prefix in _TEMP_PREFIXES):
        return True
    return any(name.endswith(suffix) for suffix in _TEMP_SUFFIXES)


class InboxEventHandler(FileSystemEventHandler):
    """Handle file creation and move events in the Inbox."""

    def __init__(self, callback: Callable[[FileEvent], None]) -> None:
        self.callback = callback

    def _handle_event(self, event: FileSystemEvent, path: Path, event_type: str) -> None:
        """Shared handler for created/moved file events.

        Both ``on_created`` and ``on_moved`` share the same logic (filter
        directories and temp files, build a :class:`FileEvent`, invoke the
        callback).  They differ only in which path attribute to read and the
        event-type label, so they delegate here.
        """
        if _is_temp_file(path):
            return
        file_event = FileEvent(
            event_id=uuid4(),
            source_path=path,
            event_type=event_type,
        )
        try:
            self.callback(file_event)
        except Exception:
            logger.exception("Inbox watcher callback failed for %s", path)

    def on_created(self, event: FileSystemEvent) -> None:
        """Process created file events."""
        if event.is_directory:
            return
        self._handle_event(event, Path(str(event.src_path)), "created")

    def on_moved(self, event: FileSystemEvent) -> None:
        """Process moved file events."""
        if event.is_directory:
            return
        self._handle_event(event, Path(str(event.dest_path)), "moved")


class InboxWatcher:
    """Watchdog-based Inbox watcher."""

    def __init__(self, inbox_path: Path, callback: Callable[[FileEvent], None]) -> None:
        self.inbox_path = inbox_path
        self.callback = callback
        self.observer = Observer()
        self.handler = InboxEventHandler(callback)
        self._started = False

    def start(self) -> None:
        """Start watching."""
        self.inbox_path.mkdir(parents=True, exist_ok=True)
        if self._started:
            return
        # Recreate the Observer to avoid stale state from a previous run.
        self.observer = Observer()
        self.observer.schedule(self.handler, str(self.inbox_path), recursive=False)
        self.observer.start()
        self._started = True
        # Scan for pre-existing files so they are not missed on startup.
        for item in sorted(self.inbox_path.iterdir()):
            if item.is_file() and not _is_temp_file(item):
                self.handler.on_created(FileCreatedEvent(src_path=str(item)))

    def stop(self) -> None:
        """Stop watching."""
        if not self._started:
            return
        self._started = False
        self.observer.stop()
        self.observer.join()
