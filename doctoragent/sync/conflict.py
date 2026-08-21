"""Conflict detection and resolution for DoctorAgent multi-device sync.

Strategies
----------
- :class:`LastWriteWins`  – resolve by mtime (simple, fast, default)
- :class:`KeepBoth`        – keep local version, rename remote copy
- :class:`ManualResolve`   – flag for user decision via callback
- :class:`CRDTMerge`       – true per-key CRDT merge (HLC + tombstones)
- :class:`ThreeWayMerge`   – 3-way merge using a common ancestor
- :class:`SemanticMerge`   – content-type aware automatic strategy selection

Building blocks
---------------
- :class:`HLC`             – Hybrid Logical Clock (per-key timestamp)
- :class:`VectorClock`     – version vector for concurrent-edit detection
- :class:`CRDTDocument`    – CRDT JSON document (insert/update/delete/merge)
- :class:`ConflictHistory` – persisted audit trail of conflicts
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, ClassVar

from doctoragent._utils import atomic_write_json
from doctoragent.sync.protocol import FileIndex

# ---------------------------------------------------------------------------
# Conflict representation
# ---------------------------------------------------------------------------


@dataclass
class Conflict:
    """Describes a conflict between local and remote versions of a file."""

    file_path: str
    local_version: dict[str, Any] = field(default_factory=dict)
    remote_version: dict[str, Any] = field(default_factory=dict)
    conflict_type: str = ""

    # Canonical conflict types
    TYPE_CONCURRENT_EDIT: ClassVar[str] = "concurrent_edit"
    TYPE_DELETE_VS_EDIT: ClassVar[str] = "delete_vs_edit"


# ---------------------------------------------------------------------------
# Conflict detector
# ---------------------------------------------------------------------------


class ConflictDetector:
    """Compare two :class:`FileIndex` snapshots and enumerate conflicts.

    The detector identifies three categories:

    * **concurrent_edit** – same file exists in both indexes with different
      content hashes.
    * **delete_vs_edit** – one side deleted the file while the other changed it
      (requires *known_files* from the previous sync state for accuracy).
    * **create_vs_create** – both sides independently created a new file at
      the same path with different content.

    Parameters
    ----------
    known_files:
        Optional mapping of ``vault_path → hash`` from the previous sync
        state.  When supplied, ``delete_vs_edit`` is distinguished from
        ``create_vs_create``.
    """

    def __init__(self, known_files: dict[str, str] | None = None) -> None:
        self._known_files = known_files or {}

    def detect(
        self,
        local_index: FileIndex,
        remote_index: FileIndex,
    ) -> list[Conflict]:
        """Return a list of :class:`Conflict` objects.

        Parameters
        ----------
        local_index:
            File index snapshot from the local device.
        remote_index:
            File index snapshot received from the peer.
        """
        local_files = local_index.files
        remote_files = remote_index.files
        conflicts: list[Conflict] = []

        all_keys = set(local_files) | set(remote_files)

        for key in all_keys:
            in_local = key in local_files
            in_remote = key in remote_files

            if in_local and in_remote:
                local_hash = local_files[key].get("hash", "")
                remote_hash = remote_files[key].get("hash", "")
                if local_hash and remote_hash and local_hash != remote_hash:
                    conflicts.append(
                        Conflict(
                            file_path=key,
                            local_version=local_files[key],
                            remote_version=remote_files[key],
                            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
                        )
                    )
            elif in_local and not in_remote:
                if key in self._known_files:
                    # Previously known → remote deleted, we edited
                    conflicts.append(
                        Conflict(
                            file_path=key,
                            local_version=local_files[key],
                            remote_version={},
                            conflict_type=Conflict.TYPE_DELETE_VS_EDIT,
                        )
                    )
                # else: local-only new file (no conflict)
            elif not in_local and in_remote:
                if key in self._known_files:
                    # Previously known → local deleted, remote edited
                    conflicts.append(
                        Conflict(
                            file_path=key,
                            local_version={},
                            remote_version=remote_files[key],
                            conflict_type=Conflict.TYPE_DELETE_VS_EDIT,
                        )
                    )
                else:
                    # Only remote has the file and it was never known locally:
                    # a new remote file, not a conflict.
                    pass

            # create_vs_create: not in known_files, exists on both sides with
            # different hashes — already handled as concurrent_edit above.

        return conflicts


# ---------------------------------------------------------------------------
# Abstract resolver
# ---------------------------------------------------------------------------


class ConflictResolver(ABC):
    """Abstract base for conflict resolution strategies."""

    @abstractmethod
    def resolve(
        self,
        conflict: Conflict,
        local_index: FileIndex,
        remote_index: FileIndex,
    ) -> dict[str, Any]:
        """Resolve a conflict.

        Returns a dict with at least ``action`` (one of ``keep_local``,
        ``keep_remote``, ``keep_both``, ``merge``) and ``reason``.
        """
        ...


# ---------------------------------------------------------------------------
# Last-write-wins (default)
# ---------------------------------------------------------------------------


class LastWriteWins(ConflictResolver):
    """Resolve conflicts by keeping whichever version has the most recent mtime.

    This is the default strategy because it is simple, predictable, and
    works for most file types.
    """

    def resolve(
        self,
        conflict: Conflict,
        local_index: FileIndex,
        remote_index: FileIndex,
    ) -> dict[str, Any]:
        local_mtime = conflict.local_version.get("mtime", 0.0) if conflict.local_version else 0.0
        remote_mtime = conflict.remote_version.get("mtime", 0.0) if conflict.remote_version else 0.0

        if conflict.conflict_type == Conflict.TYPE_DELETE_VS_EDIT:
            # If remote deleted but we have local edits → keep local
            if conflict.local_version and not conflict.remote_version:
                return {
                    "action": "keep_local",
                    "file_path": conflict.file_path,
                    "reason": "delete_vs_edit: keeping local edit",
                }
            # If local deleted but remote edited → keep remote
            return {
                "action": "keep_remote",
                "file_path": conflict.file_path,
                "reason": "delete_vs_edit: keeping remote edit",
            }

        if remote_mtime > local_mtime:
            return {
                "action": "keep_remote",
                "file_path": conflict.file_path,
                "reason": f"remote mtime {remote_mtime} > local {local_mtime}",
            }
        return {
            "action": "keep_local",
            "file_path": conflict.file_path,
            "reason": f"local mtime {local_mtime} >= remote {remote_mtime}",
        }


# ---------------------------------------------------------------------------
# Keep-both
# ---------------------------------------------------------------------------


class KeepBoth(ConflictResolver):
    """Keep the local version as-is and rename the remote version.

    The remote version is written to ``<file>.conflict.<timestamp>.<ext>``
    so both versions survive.
    """

    def resolve(
        self,
        conflict: Conflict,
        local_index: FileIndex,
        remote_index: FileIndex,
    ) -> dict[str, Any]:
        # Nanosecond timestamp + random suffix: prevents name collisions when
        # the same conflict happens multiple times within the same second, and
        # avoids predictable file names.
        ts = time.time_ns()
        rand_suffix = os.urandom(4).hex()
        # Use PurePath.stem / .suffix so multi-dot names (e.g. "archive.tar.gz")
        # and edge cases ("README", ".bashrc") are split correctly rather than
        # naively rsplitting on the last dot.
        path = PurePath(conflict.file_path)
        stem = path.stem
        suffix = path.suffix
        conflict_name = f"{stem}.conflict.{ts}.{rand_suffix}{suffix}"

        return {
            "action": "keep_both",
            "file_path": conflict.file_path,
            "conflict_path": conflict_name,
            "reason": f"both versions kept — conflict copy at {conflict_name}",
        }


# ---------------------------------------------------------------------------
# Manual resolution
# ---------------------------------------------------------------------------


class ManualResolve(ConflictResolver):
    """Flag conflicts for manual resolution.

    Returns ``action="manual"`` so the engine can invoke the conflict
    callback for a user decision.
    """

    def resolve(
        self,
        conflict: Conflict,
        local_index: FileIndex,
        remote_index: FileIndex,
    ) -> dict[str, Any]:
        return {
            "action": "manual",
            "file_path": conflict.file_path,
            "conflict_type": conflict.conflict_type,
            "local_hash": conflict.local_version.get("hash", ""),
            "remote_hash": conflict.remote_version.get("hash", ""),
            "reason": "requires manual resolution",
        }


# ---------------------------------------------------------------------------
# Hybrid Logical Clock (HLC)
# ---------------------------------------------------------------------------


class HLC:
    """Hybrid Logical Clock.

    A tuple ``(wall_ms, counter, node_id)`` that combines physical time with a
    logical counter so events are totally ordered even when the wall clock is
    unchanged.  Used as the per-key timestamp inside :class:`CRDTDocument`.

    The physical component is stored in **milliseconds** to give sub-second
    resolution while still fitting a compact JSON-serialisable integer.
    """

    __slots__ = ("wall_ms", "counter", "node_id")

    def __init__(self, wall_ms: int = 0, counter: int = 0, node_id: str = "") -> None:
        self.wall_ms = int(wall_ms)
        self.counter = int(counter)
        self.node_id = node_id

    # -- clock operations --------------------------------------------------

    def now(self, physical_ms: int) -> HLC:
        """Advance the clock for a local event and return the new timestamp."""
        if physical_ms > self.wall_ms:
            return HLC(physical_ms, 0, self.node_id)
        return HLC(self.wall_ms, self.counter + 1, self.node_id)

    def receive(self, remote: HLC, physical_ms: int) -> HLC:
        """Merge a *remote* timestamp observed from another node."""
        if physical_ms > self.wall_ms and physical_ms > remote.wall_ms:
            return HLC(physical_ms, 0, self.node_id)
        if remote.wall_ms > self.wall_ms:
            return HLC(remote.wall_ms, remote.counter + 1, self.node_id)
        if remote.wall_ms == self.wall_ms:
            return HLC(self.wall_ms, max(self.counter, remote.counter) + 1, self.node_id)
        # local wall_ms is strictly the greatest
        return HLC(self.wall_ms, self.counter + 1, self.node_id)

    # -- comparison --------------------------------------------------------

    def compare(self, other: HLC) -> int:
        """Return ``-1``/``0``/``1`` comparing ``self`` to ``other``."""
        if self.wall_ms != other.wall_ms:
            return -1 if self.wall_ms < other.wall_ms else 1
        if self.counter != other.counter:
            return -1 if self.counter < other.counter else 1
        # Tie-break on node_id for a deterministic, symmetric total order.
        if self.node_id != other.node_id:
            return -1 if self.node_id < other.node_id else 1
        return 0

    def __lt__(self, other: HLC) -> bool:
        return self.compare(other) < 0

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, HLC):
            return NotImplemented
        return self.compare(other) == 0

    def __le__(self, other: HLC) -> bool:
        return self.compare(other) <= 0

    # -- serialisation -----------------------------------------------------

    def to_tuple(self) -> list:
        """JSON-friendly representation ``[wall_ms, counter, node_id]``."""
        return [self.wall_ms, self.counter, self.node_id]

    @classmethod
    def from_tuple(cls, data: Any) -> HLC:
        if data is None:
            return cls()
        if isinstance(data, HLC):
            return data
        if isinstance(data, (list, tuple)) and len(data) >= 2:
            return cls(
                wall_ms=int(data[0]),
                counter=int(data[1]),
                node_id=str(data[2]) if len(data) > 2 else "",
            )
        if isinstance(data, dict):
            return cls(
                wall_ms=int(data.get("wall_ms", 0)),
                counter=int(data.get("counter", 0)),
                node_id=str(data.get("node_id", "")),
            )
        # Numeric fallback (legacy bare-mtime values).
        try:
            return cls(wall_ms=int(float(data)))
        except (TypeError, ValueError):
            return cls()

    @staticmethod
    def from_mtime(mtime: float, node_id: str = "") -> HLC:
        """Build an HLC from a Unix ``mtime`` (seconds, float)."""
        return HLC(wall_ms=int(float(mtime) * 1000), counter=0, node_id=node_id)

    @staticmethod
    def max(a: HLC, b: HLC) -> HLC:  # noqa: A002 – mirror builtin name
        return a if a.compare(b) >= 0 else b


# ---------------------------------------------------------------------------
# Vector clock
# ---------------------------------------------------------------------------


class VectorClock:
    """Version vector ``{node_id: counter}`` for detecting concurrent edits."""

    __slots__ = ("_clocks",)

    def __init__(self, clocks: dict[str, int] | None = None) -> None:
        self._clocks: dict[str, int] = dict(clocks) if clocks else {}

    def increment(self, node_id: str) -> None:
        self._clocks[node_id] = self._clocks.get(node_id, 0) + 1

    def merge(self, other: VectorClock) -> VectorClock:
        merged: dict[str, int] = {}
        for node, count in self._clocks.items():
            merged[node] = count
        for node, count in other._clocks.items():
            merged[node] = max(merged.get(node, 0), count)
        return VectorClock(merged)

    def compare(self, other: VectorClock) -> str:
        """Return ``before``, ``after``, ``equal`` or ``concurrent``."""
        keys = set(self._clocks) | set(other._clocks)
        less = greater = False
        for k in keys:
            a = self._clocks.get(k, 0)
            b = other._clocks.get(k, 0)
            if a < b:
                less = True
            elif a > b:
                greater = True
        if less and greater:
            return "concurrent"
        if less:
            return "before"
        if greater:
            return "after"
        return "equal"

    def to_dict(self) -> dict[str, int]:
        return dict(self._clocks)

    @classmethod
    def from_dict(cls, data: Any) -> VectorClock:
        if not isinstance(data, dict):
            return cls()
        return cls({str(k): int(v) for k, v in data.items() if v is not None})


# ---------------------------------------------------------------------------
# CRDT document (per-key LWW + tombstones + vector clock)
# ---------------------------------------------------------------------------


class CRDTDocument:
    """A JSON document backed by a Conflict-free Replicated Data Type.

    Each top-level key is stored as an *entry*::

        {"value": <any>, "timestamp": HLC_tuple, "deleted": bool}

    * ``timestamp`` is the HLC of the last write to that key, giving a true
      per-key logical clock rather than a single file-level ``mtime``.
    * ``deleted=True`` marks a *tombstone*: the key was intentionally removed
      and the tombstone must be propagated so other nodes do not resurrect it.
    * Nested ``dict`` values are merged recursively (each sub-key carries its
      own timestamp).
    * ``list`` values use add-wins OR-Set semantics (union, de-duplicated by
      value) so concurrent additions on both peers converge.

    The document also tracks a :class:`VectorClock` and an :class:`HLC` so
    concurrent modifications can be detected and ordered.
    """

    # Serialised entry keys
    F_VALUE = "value"
    F_TIMESTAMP = "timestamp"
    F_DELETED = "deleted"

    def __init__(self, node_id: str = "") -> None:
        self.node_id = node_id
        self.entries: dict[str, dict[str, Any]] = {}
        self.vector_clock = VectorClock()
        self.hlc = HLC(node_id=node_id)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def _next_hlc(self) -> HLC:
        import time as _time

        self.hlc = self.hlc.now(int(_time.time() * 1000))
        return self.hlc

    def insert(self, key: str, value: Any, hlc: HLC | None = None) -> None:
        """Insert (or update) *key* with *value*."""
        ts = hlc if hlc is not None else self._next_hlc()
        self.entries[key] = {
            self.F_VALUE: self._wrap_value(value, ts),
            self.F_TIMESTAMP: ts.to_tuple(),
            self.F_DELETED: False,
        }
        self.vector_clock.increment(self.node_id)

    # ``update`` is semantically identical to ``insert`` for LWW CRDTs.
    update = insert

    def delete(self, key: str, hlc: HLC | None = None) -> None:
        """Write a tombstone for *key*."""
        ts = hlc if hlc is not None else self._next_hlc()
        self.entries[key] = {
            self.F_VALUE: None,
            self.F_TIMESTAMP: ts.to_tuple(),
            self.F_DELETED: True,
        }
        self.vector_clock.increment(self.node_id)

    def get(self, key: str) -> Any:
        """Return the live value for *key*, or ``None`` if absent/tombstoned."""
        entry = self.entries.get(key)
        if entry is None or entry.get(self.F_DELETED):
            return None
        return self._unwrap_value(entry[self.F_VALUE])

    def keys(self) -> list[str]:
        return list(self.entries.keys())

    def live_keys(self) -> list[str]:
        return [k for k, e in self.entries.items() if not e.get(self.F_DELETED)]

    # ------------------------------------------------------------------
    # Value wrapping (recursive for nested dicts)
    # ------------------------------------------------------------------

    def _wrap_value(self, value: Any, ts: HLC) -> Any:
        """Wrap nested dicts so every sub-key carries its own timestamp."""
        if isinstance(value, dict):
            wrapped: dict[str, dict[str, Any]] = {}
            for k, v in value.items():
                wrapped[k] = {
                    self.F_VALUE: self._wrap_value(v, ts),
                    self.F_TIMESTAMP: ts.to_tuple(),
                    self.F_DELETED: False,
                }
            return wrapped
        return value

    def _unwrap_value(self, value: Any) -> Any:
        """Materialise a (possibly wrapped) value back to plain Python."""
        if isinstance(value, dict) and self._is_crdt_map(value):
            out: dict[str, Any] = {}
            for k, entry in value.items():
                if (
                    isinstance(entry, dict)
                    and self.F_VALUE in entry
                    and not entry.get(self.F_DELETED)
                ):
                    out[k] = self._unwrap_value(entry[self.F_VALUE])
            return out
        return value

    @classmethod
    def _is_crdt_map(cls, value: Any) -> bool:
        """Return True if *value* looks like a CRDT entry map."""
        if not isinstance(value, dict) or not value:
            return False
        return all(
            isinstance(v, dict) and cls.F_VALUE in v and cls.F_TIMESTAMP in v
            for v in value.values()
        )

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def merge(self, other: CRDTDocument) -> CRDTDocument:
        """Merge another document into a new converged document."""
        import time as _time

        result = CRDTDocument(node_id=self.node_id)
        result.vector_clock = self.vector_clock.merge(other.vector_clock)
        result.hlc = self.hlc.receive(other.hlc, int(_time.time() * 1000))

        all_keys = set(self.entries) | set(other.entries)
        for key in all_keys:
            le = self.entries.get(key)
            re_ = other.entries.get(key)
            if le is not None and re_ is None:
                result.entries[key] = dict(le)
                continue
            if re_ is not None and le is None:
                result.entries[key] = dict(re_)
                continue

            assert le is not None and re_ is not None
            lts = HLC.from_tuple(le.get(self.F_TIMESTAMP))
            rts = HLC.from_tuple(re_.get(self.F_TIMESTAMP))

            # Both live and both dict-valued → recurse for fine-grained merge.
            lv = le.get(self.F_VALUE)
            rv = re_.get(self.F_VALUE)
            if (
                not le.get(self.F_DELETED)
                and not re_.get(self.F_DELETED)
                and isinstance(lv, dict)
                and isinstance(rv, dict)
                and not self._is_crdt_map(lv)
                and not self._is_crdt_map(rv)
            ):
                merged_value = self._merge_plain_maps(lv, rv, lts, rts)
                result.entries[key] = {
                    self.F_VALUE: merged_value,
                    self.F_TIMESTAMP: HLC.max(lts, rts).to_tuple(),
                    self.F_DELETED: False,
                }
                continue

            if (
                not le.get(self.F_DELETED)
                and not re_.get(self.F_DELETED)
                and isinstance(lv, dict)
                and isinstance(rv, dict)
            ):
                # Both are CRDT maps (wrapped nested docs) → recurse.
                merged_map = self._merge_crdt_maps(lv, rv)
                result.entries[key] = {
                    self.F_VALUE: merged_map,
                    self.F_TIMESTAMP: HLC.max(lts, rts).to_tuple(),
                    self.F_DELETED: False,
                }
                continue

            # Lists → add-wins OR-Set (union, de-duplicated).
            if (
                not le.get(self.F_DELETED)
                and not re_.get(self.F_DELETED)
                and isinstance(lv, list)
                and isinstance(rv, list)
            ):
                merged_list = self._merge_lists(lv, rv)
                result.entries[key] = {
                    self.F_VALUE: merged_list,
                    self.F_TIMESTAMP: HLC.max(lts, rts).to_tuple(),
                    self.F_DELETED: False,
                }
                continue

            # Scalar / mismatched types → LWW by timestamp.
            winner = le if lts.compare(rts) >= 0 else re_
            result.entries[key] = dict(winner)

        return result

    @classmethod
    def _merge_crdt_maps(cls, local_map: dict, remote_map: dict) -> dict:
        merged: dict[str, dict[str, Any]] = {}
        for key in set(local_map) | set(remote_map):
            le = local_map.get(key)
            re_ = remote_map.get(key)
            if le is not None and re_ is None:
                merged[key] = dict(le)
            elif re_ is not None and le is None:
                merged[key] = dict(re_)
            else:
                assert le is not None and re_ is not None
                lts = HLC.from_tuple(le.get(cls.F_TIMESTAMP))
                rts = HLC.from_tuple(re_.get(cls.F_TIMESTAMP))
                winner = le if lts.compare(rts) >= 0 else re_
                merged[key] = dict(winner)
        return merged

    @classmethod
    def _merge_plain_maps(cls, local_map: dict, remote_map: dict, lts: HLC, rts: HLC) -> dict:
        """Merge two *plain* (non-CRDT) nested dicts per-key."""
        merged: dict[str, dict[str, Any]] = {}
        for key in set(local_map) | set(remote_map):
            lv = local_map.get(key)
            rv = remote_map.get(key)
            if lv is not None and rv is None:
                merged[key] = {
                    cls.F_VALUE: lv,
                    cls.F_TIMESTAMP: lts.to_tuple(),
                    cls.F_DELETED: False,
                }
            elif rv is not None and lv is None:
                merged[key] = {
                    cls.F_VALUE: rv,
                    cls.F_TIMESTAMP: rts.to_tuple(),
                    cls.F_DELETED: False,
                }
            else:
                winner_ts = lts if lts.compare(rts) >= 0 else rts
                winner_val = lv if lts.compare(rts) >= 0 else rv
                merged[key] = {
                    cls.F_VALUE: winner_val,
                    cls.F_TIMESTAMP: winner_ts.to_tuple(),
                    cls.F_DELETED: False,
                }
        return merged

    @staticmethod
    def _merge_lists(local_list: list, remote_list: list) -> list:
        """Add-wins OR-Set: union of both lists, preserving order, de-duplicated."""
        seen: list = []
        for item in list(local_list) + list(remote_list):
            if item not in seen:
                seen.append(item)
        return seen

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def serialize(self) -> dict[str, Any]:
        """Return the CRDT-internal representation.

        Format::

            {
                "_meta": {"node_id": ..., "vector_clock": {...}},
                "<key>": {"value": ..., "timestamp": [...], "deleted": bool},
                ...
            }
        """
        return {
            "_meta": {
                "node_id": self.node_id,
                "vector_clock": self.vector_clock.to_dict(),
            },
            **{k: dict(v) for k, v in self.entries.items()},
        }

    @classmethod
    def deserialize(cls, data: Any) -> CRDTDocument:
        if not isinstance(data, dict):
            return cls()
        meta = data.get("_meta", {}) if isinstance(data.get("_meta"), dict) else {}
        doc = cls(node_id=str(meta.get("node_id", "")))
        doc.vector_clock = VectorClock.from_dict(meta.get("vector_clock", {}))
        for key, entry in data.items():
            if key == "_meta":
                continue
            if isinstance(entry, dict) and cls.F_VALUE in entry:
                doc.entries[str(key)] = {
                    cls.F_VALUE: entry[cls.F_VALUE],
                    cls.F_TIMESTAMP: entry.get(cls.F_TIMESTAMP, [0, 0, ""]),
                    cls.F_DELETED: bool(entry.get(cls.F_DELETED, False)),
                }
        return doc

    def to_plain(self) -> dict[str, Any]:
        """Materialise the document into a plain ``{key: value}`` dict."""
        out: dict[str, Any] = {}
        for key, entry in self.entries.items():
            if entry.get(self.F_DELETED):
                continue
            out[key] = self._unwrap_value(entry.get(self.F_VALUE))
        return out

    @classmethod
    def from_plain(cls, data: Any, mtime: float, node_id: str) -> CRDTDocument:
        """Build a document from a plain dict, using *mtime* as the per-key HLC.

        This provides backward compatibility for callers (including the legacy
        :class:`CRDTMerge` resolver) that supply a plain JSON object with a
        single file-level ``mtime`` rather than full per-key CRDT metadata.
        """
        doc = cls(node_id=node_id or "node")
        ts = HLC.from_mtime(mtime, node_id or "node")
        if not isinstance(data, dict):
            return doc
        for key, value in data.items():
            doc.entries[key] = {
                cls.F_VALUE: doc._wrap_value(value, ts),
                cls.F_TIMESTAMP: ts.to_tuple(),
                cls.F_DELETED: False,
            }
        return doc


# ---------------------------------------------------------------------------
# CRDT merge (for JSON / metadata files)
# ---------------------------------------------------------------------------


class CRDTMerge(ConflictResolver):
    """Merge JSON files using a true per-key CRDT.

    Each side's JSON payload is lifted into a :class:`CRDTDocument` (using the
    file ``mtime`` as the per-key timestamp when full CRDT metadata is not
    present), the two documents are merged with per-key LWW + tombstones +
    recursive nested-map merge + OR-Set list union, and the result is
    materialised back into a plain JSON object.

    When the supplied ``data`` is already in CRDT-serialised form
    (``{key: {"value", "timestamp", "deleted"}}``) the per-key timestamps and
    tombstones are honoured directly, giving genuine CRDT convergence.
    """

    def resolve(
        self,
        conflict: Conflict,
        local_index: FileIndex,
        remote_index: FileIndex,
    ) -> dict[str, Any]:
        local_raw = conflict.local_version.get("data") if conflict.local_version else None
        remote_raw = conflict.remote_version.get("data") if conflict.remote_version else None

        local_data = self._try_parse_json(local_raw)
        remote_data = self._try_parse_json(remote_raw)

        local_mtime = conflict.local_version.get("mtime", 0.0) if conflict.local_version else 0.0
        remote_mtime = conflict.remote_version.get("mtime", 0.0) if conflict.remote_version else 0.0

        local_node = (local_index.device_id or "local") if local_index else "local"
        remote_node = (remote_index.device_id or "remote") if remote_index else "remote"

        local_doc = self._to_document(local_data, local_mtime, local_node)
        remote_doc = self._to_document(remote_data, remote_mtime, remote_node)

        merged_doc = local_doc.merge(remote_doc)
        merged = merged_doc.to_plain()

        merged_json = json.dumps(merged, sort_keys=True, indent=2)
        merged_hash = hashlib.sha256(merged_json.encode("utf-8")).hexdigest()

        return {
            "action": "merge",
            "file_path": conflict.file_path,
            "merged_data": merged,
            "merged_hash": merged_hash,
            "merged_crdt": merged_doc.serialize(),
            "reason": "CRDT per-key LWW merge (HLC + tombstones + vector clock)",
        }

    @staticmethod
    def _to_document(data: Any, mtime: float, node_id: str) -> CRDTDocument:
        """Lift parsed JSON into a :class:`CRDTDocument`.

        Detects already-serialised CRDT state (every value is an entry dict
        with ``value``+``timestamp``) and deserialises it directly so per-key
        timestamps and tombstones are preserved; otherwise falls back to
        :meth:`CRDTDocument.from_plain` using *mtime*.
        """
        if CRDTDocument._is_crdt_map(data):
            return CRDTDocument.deserialize(data)
        return CRDTDocument.from_plain(data, mtime, node_id)

    @staticmethod
    def _try_parse_json(data: Any) -> dict[str, Any]:
        if data is None:
            return {}
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}


# ---------------------------------------------------------------------------
# Three-way merge (common ancestor)
# ---------------------------------------------------------------------------

# File extensions whose contents should be treated as text for 3-way merge.
_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".log",
    ".csv",
    ".tsv",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".sh",
    ".bash",
    ".zsh",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".sql",
    ".gradle",
    ".kt",
    ".swift",
}


def _is_text_file(file_path: str) -> bool:
    """Heuristic: return True if *file_path* looks like a text file."""
    path = PurePath(file_path)
    suffix = path.suffix.lower()
    return suffix in _TEXT_EXTENSIONS


def _is_json_file(file_path: str) -> bool:
    return PurePath(file_path).suffix.lower() == ".json"


def _as_text(data: Any) -> str:
    """Coerce a conflict version's ``data`` field to text."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")
    try:
        return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(data)


class ThreeWayMerge(ConflictResolver):
    """Three-way merge using a common ancestor.

    * For **text** files: a line-level 3-way merge based on
      :class:`difflib.SequenceMatcher`.  When both sides change the same
      region, the conflict is marked with ``<<<<<<<``/``>>>>>>>`` markers so
      no data is silently lost.
    * For **JSON** files: a per-key 3-way merge.  If both sides changed the
      *same* key to *different* values, that key is flagged as a conflict.

    The common ancestor is obtained from ``ancestor_provider``, a callable
    ``file_path -> dict | None`` returning the ancestor version (in the same
    shape as a ``Conflict.local_version``).  When no ancestor is available the
    resolver degrades to a 2-way :class:`LastWriteWins` decision.
    """

    def __init__(
        self,
        ancestor_provider: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self._ancestor_provider = ancestor_provider
        self._fallback = LastWriteWins()

    def resolve(
        self,
        conflict: Conflict,
        local_index: FileIndex,
        remote_index: FileIndex,
    ) -> dict[str, Any]:
        ancestor = (
            self._ancestor_provider(conflict.file_path)
            if self._ancestor_provider is not None
            else None
        )

        if ancestor is None:
            return self._fallback.resolve(conflict, local_index, remote_index)

        if _is_json_file(conflict.file_path):
            return self._merge_json(conflict, ancestor)
        if _is_text_file(conflict.file_path):
            return self._merge_text(conflict, ancestor)
        # Binary / unknown → fall back to LWW.
        return self._fallback.resolve(conflict, local_index, remote_index)

    # ------------------------------------------------------------------
    # JSON 3-way merge
    # ------------------------------------------------------------------

    def _merge_json(self, conflict: Conflict, ancestor: dict[str, Any]) -> dict[str, Any]:
        local = CRDTMerge._try_parse_json(
            conflict.local_version.get("data") if conflict.local_version else None
        )
        remote = CRDTMerge._try_parse_json(
            conflict.remote_version.get("data") if conflict.remote_version else None
        )
        base = CRDTMerge._try_parse_json(ancestor.get("data"))

        merged: dict[str, Any] = {}
        conflicts: list[dict[str, Any]] = []
        for key in set(local) | set(remote) | set(base):
            lv = local.get(key)
            rv = remote.get(key)
            bv = base.get(key)

            local_changed = lv != bv
            remote_changed = rv != bv

            if key in local and key in remote:
                if local_changed and remote_changed and lv != rv:
                    # Both sides changed this key differently → conflict.
                    conflicts.append({"key": key, "local": lv, "remote": rv, "ancestor": bv})
                    # Default: prefer remote (deterministic); caller can
                    # resolve via conflict markers.
                    merged[key] = rv
                elif local_changed:
                    merged[key] = lv
                elif remote_changed:
                    merged[key] = rv
                else:
                    merged[key] = lv  # unchanged
            elif key in local:
                # Removed on remote.  If local changed it, keep local (edit
                # wins over delete); else propagate the delete.
                if local_changed:
                    merged[key] = lv
            elif key in remote:
                if remote_changed:
                    merged[key] = rv
            # else: deleted on both → stay deleted.

        merged_json = json.dumps(merged, sort_keys=True, indent=2)
        merged_hash = hashlib.sha256(merged_json.encode("utf-8")).hexdigest()

        if conflicts:
            return {
                "action": "merge",
                "file_path": conflict.file_path,
                "merged_data": merged,
                "merged_hash": merged_hash,
                "conflicts": conflicts,
                "reason": "3-way JSON merge with unresolved key conflicts",
            }
        return {
            "action": "merge",
            "file_path": conflict.file_path,
            "merged_data": merged,
            "merged_hash": merged_hash,
            "reason": "3-way JSON merge (clean)",
        }

    # ------------------------------------------------------------------
    # Text 3-way merge
    # ------------------------------------------------------------------

    def _merge_text(self, conflict: Conflict, ancestor: dict[str, Any]) -> dict[str, Any]:
        local_text = _as_text(
            conflict.local_version.get("data") if conflict.local_version else None
        )
        remote_text = _as_text(
            conflict.remote_version.get("data") if conflict.remote_version else None
        )
        base_text = _as_text(ancestor.get("data"))

        local_lines = local_text.splitlines(keepends=True)
        remote_lines = remote_text.splitlines(keepends=True)
        base_lines = base_text.splitlines(keepends=True)

        merged_lines, conflict_count = _diff3_merge(base_lines, local_lines, remote_lines)

        merged_text = "".join(merged_lines)
        merged_hash = hashlib.sha256(merged_text.encode("utf-8")).hexdigest()

        return {
            "action": "merge",
            "file_path": conflict.file_path,
            "merged_data": merged_text,
            "merged_hash": merged_hash,
            "conflicts": conflict_count,
            "reason": (
                f"3-way text merge ({conflict_count} conflict region(s))"
                if conflict_count
                else "3-way text merge (clean)"
            ),
        }


def _diff3_merge(base: list[str], local: list[str], remote: list[str]) -> tuple[list[str], int]:
    """Line-level 3-way merge of *local* and *remote* against *base*.

    Uses :class:`difflib.SequenceMatcher` to find base lines that survived
    unchanged on both sides (sync points).  Between sync points each side's
    content is compared:

    * identical on both sides → keep one copy,
    * one side unchanged from base → take the other side,
    * both sides changed differently → emit ``<<<<<<<``/``>>>>>>>`` conflict
      markers.

    Returns ``(merged_lines, conflict_count)``.
    """
    import difflib

    n = len(base)

    # base_to_local[i] = j when base[i] is matched to local[j], else -1.
    base_to_local = [-1] * n
    for bi, lj, size in difflib.SequenceMatcher(
        a=base, b=local, autojunk=False
    ).get_matching_blocks():
        for k in range(size):
            base_to_local[bi + k] = lj + k

    base_to_remote = [-1] * n
    for bi, rj, size in difflib.SequenceMatcher(
        a=base, b=remote, autojunk=False
    ).get_matching_blocks():
        for k in range(size):
            base_to_remote[bi + k] = rj + k

    common = [base_to_local[i] != -1 and base_to_remote[i] != -1 for i in range(n)]

    merged: list[str] = []
    conflicts = 0
    i = 0
    while i < n:
        if common[i]:
            merged.append(base[i])
            i += 1
            continue

        # Start of a changed region [i, j).
        j = i
        while j < n and not common[j]:
            j += 1

        base_slice = base[i:j]
        prev_l = base_to_local[i - 1] if i > 0 else -1
        next_l = base_to_local[j] if j < n else len(local)
        local_slice = local[prev_l + 1 : next_l]

        prev_r = base_to_remote[i - 1] if i > 0 else -1
        next_r = base_to_remote[j] if j < n else len(remote)
        remote_slice = remote[prev_r + 1 : next_r]

        if local_slice == remote_slice:
            merged.extend(local_slice)
        elif local_slice == base_slice:
            merged.extend(remote_slice)  # only remote changed
        elif remote_slice == base_slice:
            merged.extend(local_slice)  # only local changed
        else:
            merged.append("<<<<<<< local\n")
            merged.extend(local_slice)
            merged.append("=======\n")
            merged.extend(remote_slice)
            merged.append(">>>>>>> remote\n")
            conflicts += 1
        i = j

    # Trailing insertions after the last common base line.
    if n == 0 or common[n - 1]:
        last_l = -1
        last_r = -1
        for k in range(n - 1, -1, -1):
            if last_l == -1 and base_to_local[k] != -1:
                last_l = base_to_local[k]
            if last_r == -1 and base_to_remote[k] != -1:
                last_r = base_to_remote[k]
            if last_l != -1 and last_r != -1:
                break
        local_tail = local[last_l + 1 :] if last_l != -1 else local
        remote_tail = remote[last_r + 1 :] if last_r != -1 else remote
        if local_tail and remote_tail:
            if local_tail == remote_tail:
                merged.extend(local_tail)
            else:
                merged.append("<<<<<<< local\n")
                merged.extend(local_tail)
                merged.append("=======\n")
                merged.extend(remote_tail)
                merged.append(">>>>>>> remote\n")
                conflicts += 1
        elif local_tail:
            merged.extend(local_tail)
        elif remote_tail:
            merged.extend(remote_tail)

    return merged, conflicts


# ---------------------------------------------------------------------------
# Semantic merge (content-type aware strategy selection)
# ---------------------------------------------------------------------------


class SemanticMerge(ConflictResolver):
    """Auto-select a merge strategy based on content type / file extension.

    * **Encrypted/JSON documents** (``.json`` or a version carrying structured
      ``data``) → :class:`CRDTMerge` (structural, lossless).
    * **Plain text** (``.txt``, ``.md``, source code, …) →
      :class:`ThreeWayMerge` when an ancestor is available, else
      :class:`LastWriteWins`.
    * **Binary** (images, archives, …) → :class:`LastWriteWins` (cannot merge).
    * **Index files** (``index.json`` / ``.sync_index``) → keep the remote
      version (server-authoritative).
    """

    INDEX_FILENAMES = {"index.json", ".sync_index", "index.idx"}

    def __init__(
        self,
        ancestor_provider: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self._crdt = CRDTMerge()
        self._text = ThreeWayMerge(ancestor_provider=ancestor_provider)
        self._binary = LastWriteWins()

    def _classify(self, conflict: Conflict) -> str:
        name = PurePath(conflict.file_path).name.lower()
        if name in self.INDEX_FILENAMES:
            return "index"
        if _is_json_file(conflict.file_path):
            return "json"
        if _is_text_file(conflict.file_path):
            return "text"
        return "binary"

    def resolve(
        self,
        conflict: Conflict,
        local_index: FileIndex,
        remote_index: FileIndex,
    ) -> dict[str, Any]:
        kind = self._classify(conflict)

        if kind == "index":
            return {
                "action": "keep_remote",
                "file_path": conflict.file_path,
                "reason": "index file: server-authoritative",
            }

        if kind == "json":
            return self._crdt.resolve(conflict, local_index, remote_index)

        if kind == "text":
            return self._text.resolve(conflict, local_index, remote_index)

        return self._binary.resolve(conflict, local_index, remote_index)


# ---------------------------------------------------------------------------
# Conflict history (audit / learning)
# ---------------------------------------------------------------------------

# ``fcntl`` is used for cross-process safety on POSIX; unavailable on Windows.
try:
    import fcntl as _fcntl  # noqa: F401
except ImportError:  # pragma: no cover – non-POSIX platform
    _fcntl = None  # type: ignore[assignment]


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically write *payload* as JSON to *path* with mode 0o600."""
    atomic_write_json(path, payload)


class ConflictHistory:
    """Persisted record of conflicts and how they were resolved.

    Useful for audit trails and for tuning future automatic strategy
    selection.  Records are appended to a JSON file under *storage_path*.
    """

    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._history_file = self._storage_path / "conflict_history.json"
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if not self._history_file.exists():
            return []
        try:
            data = json.loads(self._history_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _save(self) -> None:
        _atomic_write_json(self._history_file, self._records)

    def record(
        self,
        conflict: Conflict,
        resolution: dict[str, Any],
    ) -> dict[str, Any]:
        """Append a conflict + resolution record and return it."""
        entry = {
            "timestamp": time.time(),
            "file_path": conflict.file_path,
            "conflict_type": conflict.conflict_type,
            "local_hash": conflict.local_version.get("hash", "") if conflict.local_version else "",
            "remote_hash": conflict.remote_version.get("hash", "")
            if conflict.remote_version
            else "",
            "action": resolution.get("action", ""),
            "reason": resolution.get("reason", ""),
            "strategy": resolution.get("strategy", ""),
        }
        with self._lock:
            self._records.append(entry)
            self._save()
        return entry

    def list_history(self, file_path: str | None = None) -> list[dict[str, Any]]:
        """Return history records, optionally filtered by *file_path*."""
        with self._lock:
            if file_path is None:
                return list(self._records)
            return [r for r in self._records if r.get("file_path") == file_path]

    def clear(self) -> int:
        """Clear all history; return the number of records removed."""
        with self._lock:
            count = len(self._records)
            self._records = []
            self._save()
            return count

    def stats(self) -> dict[str, Any]:
        """Aggregate statistics over the recorded history."""
        with self._lock:
            records = list(self._records)
        if not records:
            return {
                "total": 0,
                "by_action": {},
                "by_file": {},
                "most_conflicted_file": None,
            }
        by_action: dict[str, int] = {}
        by_file: dict[str, int] = {}
        for r in records:
            action = str(r.get("action", "unknown"))
            by_action[action] = by_action.get(action, 0) + 1
            fp = str(r.get("file_path", ""))
            if fp:
                by_file[fp] = by_file.get(fp, 0) + 1
        most_conflicted = max(by_file, key=by_file.get) if by_file else None
        return {
            "total": len(records),
            "by_action": by_action,
            "by_file": by_file,
            "most_conflicted_file": most_conflicted,
        }
