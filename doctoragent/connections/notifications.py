"""Cross-platform desktop notifications for DoctorAgent.

Provides a unified :class:`DesktopNotifier` that delegates to the
`plyer <https://github.com/kivy/plyer>`_ library, which abstracts the
platform-specific notification backends:

- **Linux**: ``notify-send`` (libnotify) via ``plyer.platforms.linux``
- **macOS**: ``osascript display notification`` via ``plyer.platforms.macos``
- **Windows**: PowerShell / WinRT toast via ``plyer.platforms.win``

Plyer is a hard dependency of this module; if it is unavailable the notifier
falls back to a no-op backend so the rest of DoctorAgent keeps working in
headless/containerised environments where desktop notifications are not
expected to fire anyway.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Plyer is an optional runtime dependency: it is only needed when a desktop
# notification actually has to be shown. We import it lazily so that
# headless/test environments without plyer can still construct a
# ``DesktopNotifier`` without an ImportError.
try:
    from plyer import notification as _plyer_notification  # type: ignore[import-not-found]

    _PLYER_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when plyer is absent
    _plyer_notification = None  # type: ignore[assignment]
    _PLYER_AVAILABLE = False


class DesktopNotifier:
    """Cross-platform desktop notification dispatcher backed by plyer.

    Send desktop notifications for key DoctorAgent events such as classification
    completion and security alerts. Each method returns ``True`` when the
    underlying plyer facade accepted the request (best-effort: plyer does not
    surface a delivery confirmation, so ``True`` only means "no exception was
    raised while dispatching").
    """

    def __init__(self) -> None:
        # Stash the facade on the instance so tests can monkeypatch it without
        # touching module-level imports.
        self._plyer = _plyer_notification

    def notify(
        self,
        title: str,
        message: str,
        urgency: str = "normal",
    ) -> bool:
        """Send a generic desktop notification via plyer.

        Parameters
        ----------
        title:
            Notification title (short summary).
        message:
            Notification body text.
        urgency:
            Urgency hint. Plyer does not expose a portable urgency field, so
            the value is mapped to the ``timeout`` parameter (critical
            notifications linger longer) and only honoured on backends that
            read it (e.g. libnotify).

        Returns
        -------
        ``True`` if the notification was dispatched without raising;
        ``False`` if plyer is unavailable or the backend raised.
        """
        if not _PLYER_AVAILABLE or self._plyer is None:
            logger.debug("plyer not available; skipping desktop notification")
            return False
        # Map our urgency levels onto a coarse timeout: critical notifications
        # stay on screen longer. ``timeout`` is the only portable knob plyer
        # exposes for urgency-like behaviour.
        timeout = 10 if urgency == "critical" else 5
        try:
            self._plyer.notify(
                title=title,
                message=message,
                app_name="DoctorAgent",
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001 - plyer backends raise a wide variety
            logger.warning("plyer notification dispatch failed", exc_info=True)
            return False
        return True

    def notify_classification_done(self, filename: str) -> bool:
        """Notify that a file has been classified.

        Parameters
        ----------
        filename:
            Name of the file that was classified.
        """
        return self.notify(
            "DoctorAgent — File Classified",
            f"'{filename}' has been processed and stored in the vault.",
            urgency="low",
        )

    def notify_security_alert(self, alert_type: str, details: str) -> bool:
        """Notify of a security-related alert.

        Parameters
        ----------
        alert_type:
            Short category of the alert (e.g. ``"unauthorized_access"``).
        details:
            Human-readable description of the alert.
        """
        return self.notify(
            f"DoctorAgent — Security Alert: {alert_type}",
            details,
            urgency="critical",
        )


def _is_plyer_available() -> bool:
    """Return True when plyer is importable (exposed for tests/diagnostics)."""
    return _PLYER_AVAILABLE


__all__ = ["DesktopNotifier", "_is_plyer_available"]


# Silence unused-import warnings for static analysers when ``Any`` is the only
# consumer of ``typing`` in this module.
_ = Any
