"""Platform connection management layer."""

from doctoragent.connections.manager import ConnectionManager
from doctoragent.connections.models import AuthMethod, Connection, PlatformType
from doctoragent.connections.notifications import DesktopNotifier

__all__ = [
    "AuthMethod",
    "Connection",
    "ConnectionManager",
    "DesktopNotifier",
    "PlatformType",
]
