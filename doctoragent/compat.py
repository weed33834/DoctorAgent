"""Cross-version compatibility shims.

The project declares ``requires-python = ">=3.10"`` but some modules use
``enum.StrEnum`` which was only added in Python 3.11. This module provides a
backport so the codebase runs unchanged on 3.10.
"""

from __future__ import annotations

import sys
from datetime import timezone
from enum import Enum

# ``datetime.UTC`` was added in Python 3.11; provide a backport for 3.10.
UTC = timezone.utc

if sys.version_info >= (3, 11):
    from enum import StrEnum as StrEnum
else:  # pragma: no cover - exercised on 3.10 only

    class StrEnum(str, Enum):
        """Backport of :class:`enum.StrEnum` for Python 3.10.

        Members are strings and ``str(member)`` returns the raw value,
        matching the 3.11 behaviour.
        """

        def __str__(self) -> str:  # type: ignore[override]
            return str(self.value)

        def _generate_next_value_(self, start, count, last_values):  # type: ignore[override]
            return self.lower()


__all__ = ["StrEnum"]
