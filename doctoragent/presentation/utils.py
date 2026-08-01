"""Shared utility functions for the presentation layer.

These helpers are pure-Python (no PyQt6 dependency) so they can be imported
safely even in headless environments where the GUI extra is not installed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def open_path(path: str | Path) -> None:
    """Open *path* with the platform's default file manager or application.

    Accepts either a ``str`` or ``pathlib.Path``.  Errors (missing binary,
    inaccessible path) are silently swallowed so that a failure to open the
    file manager never crashes the GUI.
    """
    target = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", target], check=False)
        else:
            subprocess.run(["xdg-open", target], check=False)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass


def human_size(num_bytes: int) -> str:
    """Return a human-readable file size string (e.g. ``"1.5 KB"``)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def validate_http_url(url: str) -> bool:
    """Return ``True`` if *url* is a valid ``http``/``https`` URL with a host."""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
