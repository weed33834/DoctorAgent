"""Fine-grained lifecycle hook system for document processing pipeline.

Provides hooks at every stage of the document lifecycle:
- on_receive: file enters inbox
- on_classify: before/after classification
- on_extract: before/after content extraction
- on_index: before/after indexing
- on_encrypt: before/after encryption
- on_archive: file moved to vault
- on_retrieve: file retrieved from vault
- on_decrypt: before/after decryption
- on_delete: file deleted
- on_error: any error in pipeline

Hooks can be sync or async, and can modify the processing flow.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from doctoragent.compat import StrEnum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook types
# ---------------------------------------------------------------------------


class HookType(StrEnum):
    """Every well-known point in the document lifecycle that can host hooks.

    ``PRE_*`` / ``POST_*`` hooks bracket a processing stage, while ``ON_*``
    hooks fire on a discrete event (file received, archived, retrieved,
    deleted, or an error occurring anywhere in the pipeline).
    """

    ON_RECEIVE = "on_receive"
    PRE_CLASSIFY = "pre_classify"
    POST_CLASSIFY = "post_classify"
    PRE_EXTRACT = "pre_extract"
    POST_EXTRACT = "post_extract"
    PRE_INDEX = "pre_index"
    POST_INDEX = "post_index"
    PRE_ENCRYPT = "pre_encrypt"
    POST_ENCRYPT = "post_encrypt"
    ON_ARCHIVE = "on_archive"
    ON_RETRIEVE = "on_retrieve"
    PRE_DECRYPT = "pre_decrypt"
    POST_DECRYPT = "post_decrypt"
    ON_DELETE = "on_delete"
    ON_ERROR = "on_error"


# ---------------------------------------------------------------------------
# Hook context & model
# ---------------------------------------------------------------------------


@dataclass
class HookContext:
    """Context object passed to every hook in a chain.

    Hooks read from and write to this object to influence the pipeline:

    - ``metadata`` carries stage-specific input/output data,
    - ``result`` holds the value produced by the stage (hooks may transform
      it for ``POST_*`` hooks),
    - ``error`` is set when running ``ON_ERROR`` hooks (or by a failing hook),
    - ``proceed`` lets any hook abort the rest of the chain and the pipeline
      by setting it to ``False``.

    Attributes
    ----------
    hook_type:
        The :class:`HookType` the chain is being run for.
    task_id:
        Identifier of the file-processing task the hooks apply to.
    file_path:
        Path of the file under processing, if applicable.
    metadata:
        Free-form, mutable bag of stage-specific data.
    result:
        The stage result (readable / modifiable by hooks).
    error:
        An exception captured for ``ON_ERROR`` hooks, or set by a hook that
        itself failed.
    proceed:
        When set to ``False`` by a hook, no further hooks in the chain run.
    """

    hook_type: HookType
    task_id: str
    file_path: Path | None = None
    metadata: dict[str, Any] = dc_field(default_factory=dict)
    result: Any = None
    error: Exception | None = None
    proceed: bool = True


@dataclass
class Hook:
    """A registered lifecycle hook.

    Attributes
    ----------
    name:
        Unique hook name used for enable/disable/unregister operations.
    hook_type:
        The :class:`HookType` this hook subscribes to.
    callback:
        Callable ``callback(context)`` invoked when the hook fires. May be sync
        or async; async callbacks are awaited.
    priority:
        Ordering weight. Hooks with higher priority run first; ties are broken
        by registration order.
    enabled:
        Whether the hook is currently active.
    condition:
        Optional callable ``condition(context) -> bool``. When present and
        falsy, the hook is skipped without firing.
    """

    name: str
    hook_type: HookType
    callback: Callable[[HookContext], Any]
    priority: int = 0
    enabled: bool = True
    condition: Callable[[HookContext], bool] | None = None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class LifecycleHookManager:
    """Register and run lifecycle hooks across the document pipeline.

    Hooks are grouped by :class:`HookType` and executed in priority order
    (higher first), with registration order breaking ties. Both synchronous and
    asynchronous callbacks are supported. A hook can short-circuit the entire
    chain by setting ``context.proceed = False``.

    The hook registry is guarded by a :class:`threading.Lock` so hooks can be
    registered / unregistered / toggled from any thread while a pipeline runs.
    Hook *execution*, however, is expected to happen inside the asyncio event
    loop thread of the pipeline.
    """

    def __init__(self) -> None:
        # hook_type -> list of hooks kept in registration order.
        self._by_type: dict[HookType, list[Hook]] = {}
        # hook name -> hook, for O(1) lookup and uniqueness enforcement.
        self._by_name: dict[str, Hook] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, hook: Hook) -> None:
        """Register a hook.

        If a hook with the same name already exists it is replaced (and moved
        to the end of the registration order for its type).
        """
        with self._lock:
            existing = self._by_name.get(hook.name)
            if existing is not None:
                old_list = self._by_type.get(existing.hook_type)
                if old_list is not None:
                    old_list[:] = [h for h in old_list if h.name != hook.name]
                    if not old_list:
                        self._by_type.pop(existing.hook_type, None)
                logger.debug("Replaced existing hook %r", hook.name)
            self._by_name[hook.name] = hook
            self._by_type.setdefault(hook.hook_type, []).append(hook)
        logger.debug(
            "Registered hook %r for %s (priority=%d)",
            hook.name,
            hook.hook_type,
            hook.priority,
        )

    def unregister(self, hook_name: str) -> bool:
        """Remove a hook by name. Returns ``True`` if a hook was removed."""
        with self._lock:
            hook = self._by_name.pop(hook_name, None)
            if hook is None:
                return False
            hooks = self._by_type.get(hook.hook_type)
            if hooks is not None:
                hooks[:] = [h for h in hooks if h.name != hook_name]
                if not hooks:
                    self._by_type.pop(hook.hook_type, None)
        logger.debug("Unregistered hook %r", hook_name)
        return True

    def enable(self, hook_name: str) -> bool:
        """Enable a previously registered hook. Returns ``True`` if found."""
        with self._lock:
            hook = self._by_name.get(hook_name)
            if hook is None:
                return False
            hook.enabled = True
        return True

    def disable(self, hook_name: str) -> bool:
        """Disable a previously registered hook. Returns ``True`` if found."""
        with self._lock:
            hook = self._by_name.get(hook_name)
            if hook is None:
                return False
            hook.enabled = False
        return True

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def run_hooks(self, hook_type: HookType, context: HookContext) -> HookContext:
        """Run all hooks registered for *hook_type* in priority order.

        Hooks are sorted by descending ``priority`` (registration order is the
        stable tiebreaker). Disabled hooks are skipped. A hook's optional
        ``condition`` may also skip it. Hook callbacks are awaited when async.

        If any hook sets ``context.proceed`` to ``False`` the chain stops
        immediately and the (possibly mutated) context is returned. Exceptions
        raised by individual hooks are isolated: they are logged, recorded on
        ``context.error`` (when not already set), and do not abort the chain.
        """
        context.hook_type = hook_type
        with self._lock:
            hooks = list(self._by_type.get(hook_type, []))

        # Stable sort by descending priority preserves registration order.
        hooks.sort(key=lambda h: -h.priority)

        for hook in hooks:
            if not context.proceed:
                logger.info("Hook chain for %s stopped (proceed=False)", hook_type)
                break
            if not hook.enabled:
                continue
            if hook.condition is not None:
                try:
                    if not bool(hook.condition(context)):
                        continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Hook %r condition raised %s; skipping hook",
                        hook.name,
                        exc,
                    )
                    continue

            try:
                if inspect.iscoroutinefunction(hook.callback):
                    await hook.callback(context)
                else:
                    hook.callback(context)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Hook %r raised an error", hook.name)
                if context.error is None:
                    context.error = exc

        return context

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def get_hooks(self, hook_type: HookType | None = None) -> list[Hook]:
        """Return registered hooks, optionally filtered by type."""
        with self._lock:
            if hook_type is None:
                return list(self._by_name.values())
            return list(self._by_type.get(hook_type, []))

    def get_hook_types(self) -> list[HookType]:
        """Return every :class:`HookType` that currently has at least one hook."""
        with self._lock:
            return [ht for ht, hooks in self._by_type.items() if hooks]

    def clear(self) -> None:
        """Remove all registered hooks."""
        with self._lock:
            self._by_type.clear()
            self._by_name.clear()
        logger.debug("Cleared all lifecycle hooks")
