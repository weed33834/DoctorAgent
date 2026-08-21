"""Incremental sync engine for DoctorAgent multi-device P2P sync.

Transports encrypted blobs only – no plaintext payloads leave the device.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from doctoragent._utils import atomic_write_json
from doctoragent.sync.auth import DeviceAuth
from doctoragent.sync.conflict import (
    Conflict,
    ConflictDetector,
    ConflictResolver,
    LastWriteWins,
)
from doctoragent.sync.discovery import DeviceDiscovery
from doctoragent.sync.protocol import (
    MSG_ERROR,
    FileIndex,
    SecureSyncProtocol,
    SyncMessage,
    SyncState,
)

logger = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024  # 64 KB
SYNC_STATE_FILE = ".sync_state.json"
HEADER_FMT = "!I"  # 4-byte big-endian unsigned int for length prefix
FILE_END_MARKER = 0x02

DEFAULT_SYNC_PORT = 9527

# Hard limit on a single wire frame.  Without it a malicious/buggy peer can
# advertise an enormous length and exhaust memory (OOM DoS).
MAX_FRAME_SIZE = 16 * 1024 * 1024  # 16 MB

# Files larger than this threshold automatically use streaming transfer
# (chunked read/encrypt/send) instead of loading the entire file into memory.
STREAMING_THRESHOLD = 100 * 1024 * 1024  # 100 MB

# Default chunk size for streaming file transfer.
STREAM_CHUNK_SIZE = 65536  # 64 KB

# Network timeouts (seconds).  Connections must not hang forever.
CONNECT_TIMEOUT = 10.0  # establishing a TCP connection
FRAME_TIMEOUT = 60.0  # receiving a single frame (header + body)


# ── Wire helpers ─────────────────────────────────────────────────────────────


async def _send_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Send a length-prefixed frame."""
    writer.write(struct.pack(HEADER_FMT, len(data)) + data)
    await writer.drain()


async def _recv_frame(reader: asyncio.StreamReader, timeout: float = FRAME_TIMEOUT) -> bytes:
    """Receive a length-prefixed frame.

    Enforces :data:`MAX_FRAME_SIZE` to prevent OOM attacks and applies a
    per-frame read *timeout* so a silent peer cannot hold the connection open
    indefinitely.  On either violation the caller should close the connection.
    """
    header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    length = struct.unpack(HEADER_FMT, header)[0]
    if length > MAX_FRAME_SIZE:
        raise ValueError(f"Frame size {length} exceeds maximum allowed {MAX_FRAME_SIZE}")
    return await asyncio.wait_for(reader.readexactly(length), timeout=timeout)


async def _send_message(writer: asyncio.StreamWriter, msg: SyncMessage) -> None:
    """Pack and send a SyncMessage as a length-prefixed JSON frame."""
    await _send_frame(writer, SecureSyncProtocol.pack(msg))


async def _recv_message(reader: asyncio.StreamReader) -> SyncMessage:
    """Receive and unpack a length-prefixed JSON frame into a SyncMessage."""
    raw = await _recv_frame(reader)
    return SecureSyncProtocol.unpack(raw)


def _stream_sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of *path* in fixed-size chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:  # noqa: PTH123 – chunked read below
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def _stream_hash_and_size(path: Path) -> tuple[str, int]:
    """Compute ``(sha256_hex, file_size)`` of *path* in a single streaming pass."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:  # noqa: PTH123 – chunked read below
        for chunk in iter(lambda: fh.read(STREAM_CHUNK_SIZE), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _is_within(base: Path, target: Path) -> bool:
    """Return True if *target* is *base* itself or nested below it."""
    base = base.resolve()
    target = target.resolve()
    return target == base or base in target.parents


def _atomic_write_json_file(path: Path, payload: Any) -> None:
    """Atomically write *payload* as JSON to *path* with mode 0o600."""
    atomic_write_json(path, payload)


# ── Offline operation queue ──────────────────────────────────────────────────


class OfflineOperationQueue:
    """Persistent queue of file operations performed while offline.

    Every ``create`` / ``update`` / ``delete`` is recorded as an operation
    log entry so that, on reconnection, the operations can be replayed to
    the peer in chronological order.  Operations that conflict (same file
    modified on both ends) are resolved via 3-way merge during replay.

    Storage format: a JSON file under *storage_path* / ``offline_ops.json``
    with mode ``0600``.  The file is written atomically (write-to-tmp +
    rename) so a crash never leaves a partially-written log.
    """

    OP_CREATE = "create"
    OP_UPDATE = "update"
    OP_DELETE = "delete"

    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._ops_file = storage_path / "offline_ops.json"
        self._lock = threading.Lock()
        self._operations: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if not self._ops_file.exists():
            return []
        try:
            data = json.loads(self._ops_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _save(self) -> None:
        _atomic_write_json_file(self._ops_file, self._operations)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_operation(
        self,
        op_type: str,
        file_path: str,
        content_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a file operation and return the created entry."""
        op = {
            "op_type": op_type,
            "file_path": file_path,
            "content_hash": content_hash,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }
        with self._lock:
            self._operations.append(op)
            self._save()
        return op

    def get_pending_operations(self) -> list[dict[str, Any]]:
        """Return a copy of all pending (un-replayed) operations."""
        with self._lock:
            return list(self._operations)

    def clear_operations(self, count: int | None = None) -> int:
        """Clear operations from the queue.

        If *count* is ``None`` all operations are cleared; otherwise only
        the first *count* are removed (useful for partial replay acks).
        Returns the number of operations removed.
        """
        with self._lock:
            if count is None or count >= len(self._operations):
                removed = len(self._operations)
                self._operations = []
            else:
                removed = count
                self._operations = self._operations[count:]
            self._save()
            return removed

    def replay_to_state(self, sync_state: SyncState) -> None:
        """Populate ``sync_state.pending_changes`` with queued operations.

        This bridges the offline queue with the existing :class:`SyncState`
        ``pending_changes`` field (previously unused), so the sync engine
        can report pending operations through the existing state-persistence
        path.
        """
        with self._lock:
            sync_state.pending_changes = list(self._operations)

    @property
    def pending_count(self) -> int:
        """Number of operations waiting to be replayed."""
        with self._lock:
            return len(self._operations)


# ── Sync progress reporter ───────────────────────────────────────────────────


class SyncProgressReporter:
    """Track and report sync progress across phases.

    Phases (in order)::

        discovery -> auth -> index_exchange -> diff
        -> conflict_resolve -> file_transfer -> done

    A progress callback ``(phase, progress_dict) -> None`` is invoked on
    every update, allowing the API layer (SSE / WebSocket) to push real-time
    progress to clients.

    Each phase reports:

    * ``progress_pct``  – percentage within the current phase
    * ``overall_pct``   – percentage across the entire sync
    * ``processed``     – items processed in this phase
    * ``total``         – total items in this phase
    * ``elapsed_seconds`` – time spent in the current phase
    * ``eta_seconds``   – estimated time remaining in the current phase
    """

    PHASES: list[str] = [
        "discovery",
        "auth",
        "index_exchange",
        "diff",
        "conflict_resolve",
        "file_transfer",
        "done",
    ]

    def __init__(
        self,
        callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._callback = callback
        self._phase = ""
        self._phase_start: float = 0.0
        self._total_phases = len(self.PHASES)
        self._phase_index = 0
        self._processed = 0
        self._total = 0
        self._sync_start: float = 0.0

    def start(self) -> None:
        """Begin a new sync progress tracking session."""
        self._sync_start = time.time()
        self._phase_index = 0
        self.set_phase("discovery")

    def set_phase(self, phase: str, total: int = 0) -> None:
        """Transition to a new phase."""
        if phase not in self.PHASES:
            return
        self._phase = phase
        self._phase_index = self.PHASES.index(phase)
        self._phase_start = time.time()
        self._processed = 0
        self._total = total
        self._notify()

    def update(self, processed: int, total: int | None = None) -> None:
        """Update progress within the current phase."""
        self._processed = processed
        if total is not None:
            self._total = total
        self._notify()

    def advance(self, count: int = 1) -> None:
        """Advance the processed count by *count*."""
        self._processed += count
        self._notify()

    def finish(self) -> None:
        """Mark the sync as complete."""
        self.set_phase("done")

    def _notify(self) -> None:
        if self._callback is None:
            return
        pct = 0.0
        if self._total > 0:
            pct = min(100.0, (self._processed / self._total) * 100.0)
        # Overall progress across all phases
        overall_pct = (self._phase_index / self._total_phases) * 100.0
        if self._total > 0:
            phase_weight = 100.0 / self._total_phases
            overall_pct += phase_weight * (pct / 100.0)
        elapsed = time.time() - self._phase_start if self._phase_start else 0.0
        eta = 0.0
        if self._processed > 0 and self._total > 0 and elapsed > 0:
            rate = self._processed / elapsed
            remaining = self._total - self._processed
            eta = remaining / rate if rate > 0 else 0.0
        self._callback(
            self._phase,
            {
                "phase": self._phase,
                "progress_pct": round(pct, 1),
                "overall_pct": round(overall_pct, 1),
                "processed": self._processed,
                "total": self._total,
                "elapsed_seconds": round(elapsed, 2),
                "eta_seconds": round(eta, 2),
            },
        )

    def get_progress(self) -> dict[str, Any]:
        """Return a snapshot of the current progress."""
        pct = 0.0
        if self._total > 0:
            pct = min(100.0, (self._processed / self._total) * 100.0)
        overall_pct = (self._phase_index / self._total_phases) * 100.0
        if self._total > 0:
            phase_weight = 100.0 / self._total_phases
            overall_pct += phase_weight * (pct / 100.0)
        elapsed = time.time() - self._phase_start if self._phase_start else 0.0
        eta = 0.0
        if self._processed > 0 and self._total > 0 and elapsed > 0:
            rate = self._processed / elapsed
            remaining = self._total - self._processed
            eta = remaining / rate if rate > 0 else 0.0
        return {
            "phase": self._phase,
            "progress_pct": round(pct, 1),
            "overall_pct": round(overall_pct, 1),
            "processed": self._processed,
            "total": self._total,
            "elapsed_seconds": round(elapsed, 2),
            "eta_seconds": round(eta, 2),
        }


# ── Sync statistics ──────────────────────────────────────────────────────────


class SyncStatistics:
    """Record and aggregate sync statistics for monitoring and analysis.

    **Per-sync stats:** files transferred, bytes transferred, conflicts,
    duration, status.

    **Historical stats:** average sync time, conflict frequency, failure
    rate — computed over all recorded sync sessions.

    Stored as a JSON file under *storage_path* / ``sync_stats.json`` with
    mode ``0600``.  At most 1 000 historical records are retained.
    """

    MAX_HISTORY = 1000

    def __init__(self, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._stats_file = storage_path / "sync_stats.json"
        self._lock = threading.Lock()
        self._history: list[dict[str, Any]] = self._load()
        self._current: dict[str, Any] = self._fresh_current()

    def _fresh_current(self) -> dict[str, Any]:
        return {
            "start_time": time.time(),
            "end_time": 0.0,
            "files_transferred": 0,
            "bytes_transferred": 0,
            "conflicts": 0,
            "status": "in_progress",
        }

    def _load(self) -> list[dict[str, Any]]:
        if not self._stats_file.exists():
            return []
        try:
            data = json.loads(self._stats_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _save(self) -> None:
        _atomic_write_json_file(self._stats_file, self._history)

    # ------------------------------------------------------------------
    # Per-sync recording
    # ------------------------------------------------------------------

    def start_sync(self) -> None:
        """Begin recording a new sync session."""
        with self._lock:
            self._current = self._fresh_current()

    def record_file(self, size: int) -> None:
        """Record a successfully transferred file of *size* bytes."""
        with self._lock:
            self._current["files_transferred"] += 1
            self._current["bytes_transferred"] += size

    def record_conflict(self, count: int = 1) -> None:
        """Record *count* conflicts encountered during the sync."""
        with self._lock:
            self._current["conflicts"] += count

    def finish_sync(self, status: str = "ok") -> None:
        """Finalise the current sync and append it to history."""
        with self._lock:
            self._current["end_time"] = time.time()
            self._current["status"] = status
            self._history.append(dict(self._current))
            if len(self._history) > self.MAX_HISTORY:
                self._history = self._history[-self.MAX_HISTORY :]
            self._save()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_current(self) -> dict[str, Any]:
        """Return the in-progress sync stats."""
        with self._lock:
            data = dict(self._current)
        if data["end_time"] == 0.0:
            data["elapsed_seconds"] = round(time.time() - data["start_time"], 2)
        else:
            data["elapsed_seconds"] = round(data["end_time"] - data["start_time"], 2)
        return data

    def get_summary(self) -> dict[str, Any]:
        """Return aggregate statistics over all historical sync sessions."""
        with self._lock:
            history = list(self._history)
        if not history:
            return {
                "total_syncs": 0,
                "avg_duration_seconds": 0.0,
                "avg_files_transferred": 0.0,
                "avg_bytes_transferred": 0.0,
                "total_conflicts": 0,
                "conflict_rate": 0.0,
                "failure_rate": 0.0,
            }
        total = len(history)
        durations = [h.get("end_time", 0.0) - h.get("start_time", 0.0) for h in history]
        files = [h.get("files_transferred", 0) for h in history]
        bytes_ = [h.get("bytes_transferred", 0) for h in history]
        conflicts = sum(h.get("conflicts", 0) for h in history)
        failures = sum(1 for h in history if h.get("status") != "ok")
        return {
            "total_syncs": total,
            "avg_duration_seconds": round(sum(durations) / total, 2),
            "avg_files_transferred": round(sum(files) / total, 2),
            "avg_bytes_transferred": round(sum(bytes_) / total, 2),
            "total_conflicts": conflicts,
            "conflict_rate": round(conflicts / total, 4),
            "failure_rate": round(failures / total, 4),
        }

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent *limit* sync records."""
        with self._lock:
            return list(self._history[-limit:])


# ── Sync Engine ──────────────────────────────────────────────────────────────


class SyncEngine:
    """Incremental P2P file synchronisation engine.

    Parameters
    ----------
    vault_path:
        Root directory of the vault whose contents are synchronised.
    protocol:
        Pre-configured :class:`SecureSyncProtocol` for message signing
        and transport encryption.
    discovery:
        Device discovery instance used to locate peers on the LAN.
    auth:
        Device authorisation manager that holds per-peer shared secrets.
    vault_key:
        32-byte AES-256 key used to encrypt ``.sync_state.json`` at rest.
        Defaults to the protocol's shared secret if omitted.
    progress_callback:
        Optional ``callable(phase: str, progress: dict) -> None`` invoked
        on every progress update during a sync.  Connect this to an SSE /
        WebSocket push channel for real-time UI updates.
    """

    def __init__(
        self,
        vault_path: Path,
        protocol: SecureSyncProtocol,
        discovery: DeviceDiscovery,
        auth: DeviceAuth,
        vault_key: bytes | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.vault_path = vault_path
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self.protocol = protocol
        self.discovery = discovery
        self.auth = auth
        self.vault_key = vault_key if vault_key is not None else protocol.shared_secret

        self._sync_state: SyncState = SyncState()
        self._state_path = vault_path / SYNC_STATE_FILE
        self._load_state()

        self._server: asyncio.AbstractServer | None = None
        self._running = False
        self._conflict_callback: Callable[[Conflict], str | None] | None = None
        self._conflict_resolver: ConflictResolver | None = None

        # ── Enterprise-grade extensions ────────────────────────────────
        # Metadata directory for offline-ops queue and sync statistics.
        self._meta_dir = vault_path / ".sync_meta"
        self._offline_queue = OfflineOperationQueue(self._meta_dir)
        self._progress_reporter = SyncProgressReporter(callback=progress_callback)
        self._statistics = SyncStatistics(self._meta_dir)

        # Forward secrecy: per-session ephemeral key.  When set (via
        # :meth:`_establish_session_key`) this overrides ``vault_key`` for
        # transport encryption so that compromise of the long-term key does
        # not reveal past sync traffic.
        self._session_key: bytes | None = None

    # ------------------------------------------------------------------
    # Conflict resolution callback
    # ------------------------------------------------------------------

    def set_conflict_callback(self, callback: Callable[[Conflict], str | None]) -> None:
        """Register a callback invoked for each unresolved conflict.

        The callback receives a :class:`Conflict` and may return one of
        ``"keep_local"``, ``"keep_remote"``, ``"keep_both"``, ``"merge"``,
        or ``None`` to defer to the configured strategy.
        """
        self._conflict_callback = callback

    def set_conflict_resolver(self, resolver: ConflictResolver) -> None:
        """Set the default conflict resolution strategy."""
        self._conflict_resolver = resolver

    # ------------------------------------------------------------------
    # File index
    # ------------------------------------------------------------------

    async def build_index(self) -> FileIndex:
        """Walk the vault and produce a :class:`FileIndex` snapshot.

        Files are hashed with a streaming SHA-256 (chunked ``update``) so the
        entire file is never held in memory at once.
        """

        def _scan() -> dict[str, dict[str, Any]]:
            files: dict[str, dict[str, Any]] = {}
            for root, _, filenames in os.walk(self.vault_path):
                for name in filenames:
                    if name == SYNC_STATE_FILE:
                        continue
                    full = Path(root) / name
                    try:
                        st = full.stat()
                    except OSError:
                        continue
                    try:
                        file_hash = _stream_sha256(full)
                    except OSError:
                        continue
                    rel = str(full.relative_to(self.vault_path)).replace("\\", "/")
                    files[rel] = {
                        "hash": file_hash,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "device_id": self.protocol.device_id,
                    }
            return files

        files = await asyncio.to_thread(_scan)
        return FileIndex(
            files=files,
            snapshot_time=time.time(),
            device_id=self.protocol.device_id,
        )

    async def detect_changes(
        self, old_index: FileIndex, new_index: FileIndex
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        """Return ``(added, modified, deleted)`` lists.

        Comparison is based on content hash (SHA-256), not mtime, to avoid
        clock-skew false positives.  Entries in *added*/*modified* retain the
        ``vault_path`` key so callers can locate the file on disk.
        """
        old_files = old_index.files
        new_files = new_index.files

        old_keys = set(old_files)
        new_keys = set(new_files)

        added: list[dict[str, Any]] = []
        modified: list[dict[str, Any]] = []
        deleted: list[str] = list(old_keys - new_keys)

        for key in new_keys & old_keys:
            if new_files[key]["hash"] != old_files[key]["hash"]:
                modified.append({"vault_path": key, **new_files[key]})
        for key in new_keys - old_keys:
            added.append({"vault_path": key, **new_files[key]})

        return added, modified, deleted

    # ------------------------------------------------------------------
    # Sync server
    # ------------------------------------------------------------------

    async def start_sync_server(
        self, host: str = "127.0.0.1", port: int = DEFAULT_SYNC_PORT
    ) -> None:
        """Start an asyncio TCP server that handles incoming sync requests.

        Defaults to ``127.0.0.1`` (loopback only).  Binding to a public
        address must be explicit so the sync port is never accidentally
        exposed.
        """
        self._server = await asyncio.start_server(self._handle_peer_connection, host, port)
        self._running = True
        logger.info("Sync server listening on %s:%d", host, port)

    async def stop_sync_server(self) -> None:
        """Stop the sync server."""
        self._running = False
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_peer_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle an incoming peer connection."""
        peer_addr = writer.get_extra_info("peername")
        logger.info("Incoming sync connection from %s", peer_addr)
        try:
            await self._sync_as_server(reader, writer)
        except Exception:
            logger.exception("Error handling peer connection")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _sync_as_server(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Server-side sync: receive peer index, push changes, pull updates."""

        # 1. Receive peer hello
        hello_msg = await _recv_message(reader)
        peer_id = hello_msg.sender_device_id

        # Verify auth.  We resolve the shared secret first; if the peer is not
        # authorised (or has no usable secret) we cannot construct a
        # ``peer_proto`` to sign an error with, so we send an *unsigned* error
        # frame and close.  For an authenticated peer whose signature/replay
        # check fails we send a *signed* error before closing (Sync-28).
        peer_secret = (
            self.auth.get_shared_secret(peer_id) if self.auth.is_authorized(peer_id) else None
        )
        if peer_secret is None:
            logger.warning("Rejected unauthorised peer %s", peer_id)
            await self._send_unsigned_error(writer, "unauthorised")
            return

        peer_proto = SecureSyncProtocol(peer_id, peer_secret)
        if not peer_proto.verify_message(hello_msg) or not peer_proto.check_replay(hello_msg):
            logger.warning("Hello message signature/replay invalid from %s", peer_id)
            await self._send_signed_error(writer, peer_proto, "invalid signature")
            return

        # 2. Send our hello (signed with the peer-specific shared secret)
        our_hello = self._sign_for_peer(self.protocol.create_hello(), peer_proto)
        await _send_message(writer, our_hello)

        # 3. Receive peer index
        index_msg = await _recv_message(reader)
        if not peer_proto.verify_message(index_msg) or not peer_proto.check_replay(index_msg):
            logger.warning("Index signature/replay invalid from %s", peer_id)
            await self._send_signed_error(writer, peer_proto, "invalid signature")
            return

        peer_index_data: dict[str, Any] = index_msg.payload
        peer_index = FileIndex.from_dict(peer_index_data)
        local_index = await self.build_index()

        # 4. Send our index
        our_index_msg = self.protocol.create_hello()
        our_index_msg.message_type = "sync_response"
        our_index_msg.payload = local_index.to_dict()
        self._sign_for_peer(our_index_msg, peer_proto)
        await _send_message(writer, our_index_msg)

        # 5. Compute diffs and detect conflicts.  Supplying ``known_files``
        #    from the previous sync state lets the detector flag
        #    delete-vs-edit situations instead of silently dropping them
        #    (Sync-21).
        added, modified, deleted = await self.detect_changes(peer_index, local_index)
        detector = ConflictDetector(known_files=self._sync_state.known_files)
        conflicts = detector.detect(local_index, peer_index)

        # Resolve and *execute* conflict actions (Sync-15).
        peer_addr = self._peer_addr(peer_id)
        for conflict in conflicts:
            result = self._resolve_conflict(conflict, local_index, peer_index)
            try:
                await self._apply_conflict_resolution(conflict, result, peer_addr, peer_proto)
            except Exception:
                logger.exception("Failed to apply conflict resolution for %s", conflict.file_path)

        # 6. Send ACK, then receive pushed files from peer
        ack = self._sign_for_peer(self.protocol.create_ack(peer_proto.device_id), peer_proto)
        await _send_message(writer, ack)

        # Peer pushes files that are new/modified on their side
        while True:
            meta_raw = await _recv_frame(reader)
            if len(meta_raw) == 1 and meta_raw[0] == FILE_END_MARKER:
                break
            # Peer frame data is untrusted: on parse failure or missing fields
            # skip the file and drain its data frames to keep the stream in sync,
            # so a single malformed frame does not abort the whole connection.
            try:
                file_meta = json.loads(meta_raw)
                vault_path = file_meta["vault_path"]
                file_hash = file_meta["hash"]
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("Malformed file metadata frame; draining and skipping")
                await self._drain_file_stream(reader)
                continue
            try:
                await self._receive_file(reader, vault_path, file_hash)
                logger.info("Received file: %s", vault_path)
            except Exception:
                logger.exception("Failed to receive file %s", vault_path)

        # 7. Push our changes to peer
        for entry in added + modified:
            rel = entry.get("vault_path", "")
            # An empty vault_path resolves to the vault root (where _send_file
            # would fail to read a directory), and an absolute path escapes the
            # vault boundary — both are skipped and logged.
            if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
                logger.warning("Skipping entry with invalid vault_path: %r", rel)
                continue
            full_path = self.vault_path / rel
            await self._send_file(writer, full_path, rel)

        # Mark end of pushes
        writer.write(struct.pack(HEADER_FMT, 1) + bytes([FILE_END_MARKER]))
        await writer.drain()

        # 8. Update sync state — record the union of known file hashes so the
        #    next round can detect delete-vs-edit (Sync-21).
        self._sync_state.last_sync_time[peer_id] = time.time()
        self._sync_state.known_files = self._merge_known_files(local_index, peer_index)
        self.auth.touch_device(peer_id)
        self._save_state()

    # ------------------------------------------------------------------
    # Server-side protocol helpers
    # ------------------------------------------------------------------

    async def _send_unsigned_error(self, writer: asyncio.StreamWriter, reason: str) -> None:
        """Send an error frame without an HMAC signature.

        For unauthenticated peers we hold no shared secret to sign with, so
        the error is sent unsigned.  Recipients must treat an unsigned error
        as informational only and never act on its payload.
        """
        err = SyncMessage(
            version=1,
            message_id=SecureSyncProtocol._new_message_id(),
            message_type=MSG_ERROR,
            sender_device_id=self.protocol.device_id,
            timestamp=time.time(),
            payload={"error": reason},
        )
        await _send_message(writer, err)

    async def _send_signed_error(
        self,
        writer: asyncio.StreamWriter,
        peer_proto: SecureSyncProtocol,
        reason: str,
    ) -> None:
        """Send a signed error frame to an authenticated peer."""
        err = peer_proto.create_error(reason)
        await _send_message(writer, err)

    def _sign_for_peer(self, msg: SyncMessage, peer_proto: SecureSyncProtocol) -> SyncMessage:
        """Re-sign *msg* with the shared secret of the given peer pair.

        Messages built via ``self.protocol.create_*`` are signed with our own
        protocol's secret.  When speaking to a peer the HMAC must be produced
        with the shared secret that peer holds for us, so the peer can verify
        it.  ``sign_message`` mutates and returns *msg* in place.
        """
        return peer_proto.sign_message(msg)

    def _peer_addr(self, peer_id: str) -> tuple[str, int] | str:
        """Resolve a peer's network address from the discovery cache.

        Returns ``(ip, port)`` when the peer is currently visible via
        discovery, otherwise the bare *peer_id* (enough for log lines).
        """
        for peer in self.discovery.get_peers():
            if peer.get("device_id") == peer_id:
                return (peer["ip"], peer["port"])
        return peer_id

    async def _apply_conflict_resolution(
        self,
        conflict: Conflict,
        result: dict[str, Any],
        peer_addr: tuple[str, int] | str,
        peer_proto: SecureSyncProtocol,
    ) -> None:
        """Execute a conflict-resolution decision on the local vault.

        Only actions satisfiable from local state are applied here:

        * ``keep_both``  – set the local copy aside under a conflict name so
          the incoming remote version lands at the original path.
        * ``merge``      – write the merged payload produced by the resolver.

        ``keep_local`` and ``keep_remote`` need no local filesystem change at
        this point: the peer's subsequent push (step 6) overwrites the local
        copy for ``keep_remote``, and ``keep_local`` leaves the local copy in
        place for our push in step 7.
        """
        action = result.get("action", "")
        file_path = conflict.file_path
        local_full = self.vault_path / file_path

        if action == "keep_both":
            conflict_name = result.get("conflict_path")
            if not conflict_name:
                logger.warning("keep_both decision for %s missing conflict_path", file_path)
                return
            target = self.vault_path / str(conflict_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if local_full.exists():
                    local_full.replace(target)
            except OSError:
                logger.exception("Failed to set aside conflict copy %s", file_path)
            return

        if action == "merge":
            merged_data = result.get("merged_data")
            if merged_data is None:
                logger.warning("merge decision for %s missing merged_data", file_path)
                return
            local_full.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(merged_data, sort_keys=True, indent=2).encode("utf-8")
            tmp = local_full.with_suffix(local_full.suffix + ".tmp")
            try:
                tmp.write_bytes(payload)
                tmp.replace(local_full)
            except OSError:
                logger.exception("Failed to write merged content for %s", file_path)
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            return

        logger.debug("Conflict %s resolved as %s with peer %s", file_path, action, peer_addr)

    def _merge_known_files(self, local_index: FileIndex, peer_index: FileIndex) -> dict[str, str]:
        """Build the union of file hashes from both sync indexes.

        Stored in ``SyncState.known_files`` so the next round can tell a
        delete-vs-edit apart from a create-vs-create.  When a path exists on
        both sides the local hash wins; a later edit still flips the hash and
        is detected next time.
        """
        merged: dict[str, str] = {}
        for path, meta in peer_index.files.items():
            merged[path] = str(meta.get("hash", ""))
        for path, meta in local_index.files.items():
            merged[path] = str(meta.get("hash", ""))
        return merged

    # ------------------------------------------------------------------
    # Peer sync (client side)
    # ------------------------------------------------------------------

    async def sync_with_peer(self, peer_addr: tuple[str, int]) -> dict[str, Any]:
        """Perform a full sync with a remote peer.

        Returns a summary dict with ``status``, ``pulled``, ``pushed``,
        and ``conflicts`` keys.
        """
        summary: dict[str, Any] = {
            "status": "error",
            "pulled": 0,
            "pushed": 0,
            "conflicts": 0,
        }

        try:
            reader, writer = await asyncio.open_connection(*peer_addr)
        except OSError as exc:
            logger.error("Failed to connect to %s:%d: %s", *peer_addr, exc)
            summary["error"] = str(exc)
            return summary

        try:
            # 1. Send hello
            hello = self.protocol.create_hello()
            await _send_message(writer, hello)

            # 2. Receive peer hello
            peer_hello = await _recv_message(reader)
            peer_id = peer_hello.sender_device_id

            if not self.auth.is_authorized(peer_id):
                logger.warning("Peer %s not authorised", peer_id)
                summary["error"] = "unauthorised"
                return summary

            peer_secret = self.auth.get_shared_secret(peer_id)
            if peer_secret is None:
                summary["error"] = "no_shared_secret"
                return summary

            peer_proto = SecureSyncProtocol(peer_id, peer_secret)
            # The client side needs replay protection too: call check_replay
            # after verify_message so an attacker cannot replay a previously
            # valid message to the client and corrupt file consistency.
            if not peer_proto.verify_message(peer_hello) or not peer_proto.check_replay(peer_hello):
                logger.warning("Peer hello signature/replay invalid from %s", peer_id)
                summary["error"] = "invalid_signature"
                return summary

            # 3. Send our index
            local_index = await self.build_index()
            our_index_msg = self.protocol.create_hello()
            our_index_msg.message_type = "sync_response"
            our_index_msg.payload = local_index.to_dict()
            self.protocol.sign_message(our_index_msg)
            await _send_message(writer, our_index_msg)

            # 4. Receive peer index
            peer_index_msg = await _recv_message(reader)
            # The peer index message must also be signature/replay checked to
            # avoid replaying a historical index.
            if not peer_proto.verify_message(peer_index_msg) or not peer_proto.check_replay(
                peer_index_msg
            ):
                logger.warning("Peer index signature/replay invalid from %s", peer_id)
                summary["error"] = "invalid_signature"
                return summary

            peer_index_data: dict[str, Any] = peer_index_msg.payload
            peer_index = FileIndex.from_dict(peer_index_data)

            # 5. Compute diffs and conflicts
            added, modified, deleted = await self.detect_changes(local_index, peer_index)
            detector = ConflictDetector()
            conflicts = detector.detect(local_index, peer_index)

            for conflict in conflicts:
                self._resolve_conflict(conflict, local_index, peer_index)

            summary["conflicts"] = len(conflicts)

            # 6. Wait for ACK then pull new files from peer
            try:
                await _recv_message(reader)
            except Exception:
                pass

            # 7. Pull files that exist on peer but not locally (remote has, we don't)
            remote_added = added
            pulled = 0
            for entry in remote_added:
                vault_path = entry.get("vault_path", "")
                try:
                    success = await self.pull_file(peer_addr, vault_path)
                    if success:
                        pulled += 1
                except Exception:
                    logger.exception("Failed to pull %s", vault_path)
            summary["pulled"] = pulled

            # 8. Push local changes to peer
            pushed = 0
            for entry in added + modified:
                full_path = self.vault_path / entry.get("vault_path", "")
                try:
                    success = await self.push_file(peer_addr, full_path)
                    if success:
                        pushed += 1
                except Exception:
                    logger.exception("Failed to push %s", entry.get("vault_path"))
            summary["pushed"] = pushed

            # Mark end of pushes
            writer.write(struct.pack(HEADER_FMT, 1) + bytes([FILE_END_MARKER]))
            await writer.drain()

            # 9. Update sync state
            self._sync_state.last_sync_time[peer_id] = time.time()
            self.auth.touch_device(peer_id)
            self._save_state()

            summary["status"] = "ok"

        except Exception as exc:
            logger.exception("Sync with %s:%d failed", *peer_addr)
            summary["error"] = str(exc)
        finally:
            writer.close()
            await writer.wait_closed()

        return summary

    # ------------------------------------------------------------------
    # File push / pull
    # ------------------------------------------------------------------

    @property
    def _transport_key(self) -> bytes:
        """Return the effective transport encryption key.

        When a forward-secret session key has been established via
        :meth:`_establish_session_key`, it takes precedence over the
        long-term ``vault_key`` so that compromise of the long-term key
        cannot decrypt past sync traffic.
        """
        return self._session_key if self._session_key is not None else self.vault_key

    async def push_file(self, peer_addr: tuple[str, int], vault_path: Path) -> bool:
        """Push a single file to a peer device."""

        try:
            reader, writer = await asyncio.open_connection(*peer_addr)
        except OSError:
            return False

        try:
            rel_path = str(vault_path.relative_to(self.vault_path)).replace("\\", "/")
            result = await self._send_file(writer, vault_path, rel_path)
            return result
        finally:
            writer.close()
            await writer.wait_closed()

    async def _send_file(
        self, writer: asyncio.StreamWriter, full_path: Path, rel_path: str
    ) -> bool:
        """Send file content in encrypted chunks.

        Files larger than :data:`STREAMING_THRESHOLD` are sent via
        :meth:`_send_file_streaming` (chunked read + encrypt + send, never
        holding the whole file in memory).  Smaller files use the original
        in-memory path for lower latency.
        """
        try:
            file_size = full_path.stat().st_size
        except OSError:
            return False

        if file_size > STREAMING_THRESHOLD:
            return await self._send_file_streaming(writer, full_path, rel_path)

        try:
            data = full_path.read_bytes()
        except OSError:
            return False

        file_hash = hashlib.sha256(data).hexdigest()
        key = self._transport_key

        # Send metadata
        meta = json.dumps(
            {
                "vault_path": rel_path,
                "hash": file_hash,
                "size": len(data),
            }
        ).encode("utf-8")
        await _send_frame(writer, meta)

        # Send chunks
        for offset in range(0, len(data), CHUNK_SIZE):
            chunk = data[offset : offset + CHUNK_SIZE]
            encrypted = self.protocol.encrypt_transport(chunk, key)
            await _send_frame(writer, encrypted)

        # Send zero-length frame as end-of-file marker
        await _send_frame(writer, b"")

        # Record statistics
        self._statistics.record_file(len(data))
        return True

    async def _send_file_streaming(
        self,
        writer: asyncio.StreamWriter,
        full_path: Path,
        rel_path: str,
        chunk_size: int = STREAM_CHUNK_SIZE,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Stream a file to *writer* in fixed-size encrypted chunks.

        The file is read in *chunk_size* blocks (default 64 KB), each block
        is encrypted individually with AES-256-GCM and sent as a separate
        length-prefixed frame.  The entire file is **never** loaded into
        memory at once, making this suitable for multi-gigabyte files.

        Parameters
        ----------
        writer:
            The asyncio stream writer connected to the peer.
        full_path:
            Absolute path of the file on disk.
        rel_path:
            Vault-relative path sent in the metadata frame.
        chunk_size:
            Read/encrypt block size (default 64 KB).
        progress_callback:
            Optional ``callback(transferred_bytes, total_bytes)`` invoked
            after every chunk.  Connect this to a progress bar or SSE push.
        """
        key = self._transport_key

        # Compute hash and size in a single streaming pass.
        try:
            file_hash, file_size = await asyncio.to_thread(_stream_hash_and_size, full_path)
        except OSError:
            return False

        # Send metadata (includes size so the receiver can pre-allocate /
        # report progress even before the first data chunk arrives).
        meta = json.dumps(
            {
                "vault_path": rel_path,
                "hash": file_hash,
                "size": file_size,
                "streaming": True,
            }
        ).encode("utf-8")
        await _send_frame(writer, meta)

        transferred = 0
        h = hashlib.sha256()

        try:
            with open(full_path, "rb") as fh:  # noqa: PTH123
                while True:
                    chunk = await asyncio.to_thread(fh.read, chunk_size)
                    if not chunk:
                        break
                    h.update(chunk)
                    encrypted = self.protocol.encrypt_transport(chunk, key)
                    await _send_frame(writer, encrypted)
                    transferred += len(chunk)
                    if progress_callback is not None:
                        progress_callback(transferred, file_size)
                    self._progress_reporter.advance(len(chunk))
        except OSError:
            return False

        # End-of-file marker
        await _send_frame(writer, b"")

        # Record statistics
        self._statistics.record_file(file_size)
        return True

    def _safe_vault_dest(self, vault_path: str) -> Path:
        """Resolve a peer-supplied ``vault_path`` to a path inside the vault root.

        Rejects empty strings, absolute paths, and any path that escapes the
        vault boundary after resolution (e.g. via ``..`` segments). The
        returned path is guaranteed to be :func:`_is_within` ``self.vault_path``.
        """
        if not vault_path or not isinstance(vault_path, str):
            raise ValueError("vault_path must be a non-empty string")
        if Path(vault_path).is_absolute():
            raise ValueError("vault_path must not be absolute")
        dest = self.vault_path / vault_path
        if not _is_within(self.vault_path, dest):
            raise ValueError("vault_path escapes the vault root")
        return dest

    async def pull_file(self, peer_addr: tuple[str, int], vault_path: str) -> bool:
        """Pull a single file from a peer and write it atomically."""
        try:
            reader, writer = await asyncio.open_connection(*peer_addr)
        except OSError:
            return False

        try:
            # Request the file
            request = self.protocol.create_sync_request(peer_device_id="", since=0.0)
            request.payload = {"action": "pull", "vault_path": vault_path}
            self.protocol.sign_message(request)
            await _send_message(writer, request)

            # Receive metadata
            meta_raw = await _recv_frame(reader)
            file_meta = json.loads(meta_raw)
            expected_hash = file_meta["hash"]
            expected_size = file_meta.get("size", 0)

            # Use streaming receive for large files to avoid loading the
            # entire payload into memory.
            if expected_size > STREAMING_THRESHOLD:
                success = await self._receive_file_streaming(
                    reader, vault_path, expected_hash, expected_size
                )
                if success:
                    self._statistics.record_file(expected_size)
                return success

            # Receive and reassemble chunks (small-file path)
            key = self._transport_key
            chunks: list[bytes] = []
            while True:
                chunk_raw = await _recv_frame(reader)
                if not chunk_raw:
                    break
                chunk = self.protocol.decrypt_transport(chunk_raw, key)
                chunks.append(chunk)

            body = b"".join(chunks)
            actual_hash = hashlib.sha256(body).hexdigest()
            if actual_hash != expected_hash:
                logger.error(
                    "Hash mismatch for %s: expected %s, got %s",
                    vault_path,
                    expected_hash,
                    actual_hash,
                )
                return False

            # Atomic write
            dest = self._safe_vault_dest(vault_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(dest)

            self._statistics.record_file(len(body))
            return True

        finally:
            writer.close()
            await writer.wait_closed()

    async def _receive_file(
        self,
        reader: asyncio.StreamReader,
        vault_path: str,
        expected_hash: str,
    ) -> bool:
        """Receive a file from a stream and write it atomically."""
        key = self._transport_key
        chunks: list[bytes] = []
        while True:
            chunk_raw = await _recv_frame(reader)
            if not chunk_raw:
                break
            chunk = self.protocol.decrypt_transport(chunk_raw, key)
            chunks.append(chunk)

        body = b"".join(chunks)
        actual_hash = hashlib.sha256(body).hexdigest()
        if actual_hash != expected_hash:
            logger.error(
                "Hash mismatch for %s: expected %s, got %s",
                vault_path,
                expected_hash,
                actual_hash,
            )
            return False

        dest = self._safe_vault_dest(vault_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(body)
        tmp.replace(dest)

        self._statistics.record_file(len(body))
        return True

    async def _receive_file_streaming(
        self,
        reader: asyncio.StreamReader,
        vault_path: str,
        expected_hash: str,
        expected_size: int,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Receive a file in encrypted chunks and stream it to disk.

        Each frame is decrypted and written immediately to a temporary file
        so the full file content is never held in memory.  After all chunks
        are received the SHA-256 is verified and the temp file is atomically
        renamed to the final destination.

        Parameters
        ----------
        reader:
            The asyncio stream reader connected to the peer.
        vault_path:
            Vault-relative path for the destination file.
        expected_hash:
            SHA-256 hex digest the reassembled file must match.
        expected_size:
            Total file size in bytes (from metadata) for progress reporting.
        progress_callback:
            Optional ``callback(transferred_bytes, total_bytes)`` invoked
            after every chunk.
        """
        key = self._transport_key
        dest = self._safe_vault_dest(vault_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")

        h = hashlib.sha256()
        transferred = 0

        try:
            with open(tmp, "wb") as fh:  # noqa: PTH123
                while True:
                    chunk_raw = await _recv_frame(reader)
                    if not chunk_raw:
                        break
                    chunk = self.protocol.decrypt_transport(chunk_raw, key)
                    h.update(chunk)
                    fh.write(chunk)
                    transferred += len(chunk)
                    if progress_callback is not None:
                        progress_callback(transferred, expected_size)
        except (OSError, Exception):
            # Clean up partial file on error.
            try:
                tmp.unlink()
            except OSError:
                pass
            return False

        actual_hash = h.hexdigest()
        if actual_hash != expected_hash:
            logger.error(
                "Hash mismatch for %s: expected %s, got %s",
                vault_path,
                expected_hash,
                actual_hash,
            )
            try:
                tmp.unlink()
            except OSError:
                pass
            return False

        tmp.replace(dest)
        return True

    async def _drain_file_stream(self, reader: asyncio.StreamReader) -> None:
        """Read and discard the current file's remaining data frames until an empty frame.

        The empty frame is the file EOF marker. Used to keep the stream in sync
        after a malformed metadata frame so subsequent files can still be
        received. An encrypted chunk always contains at least nonce +
        ciphertext + tag and is never empty, so an empty frame can only be the
        end-of-file marker.
        """
        while True:
            chunk_raw = await _recv_frame(reader)
            if not chunk_raw:
                break

    # ------------------------------------------------------------------
    # One-shot sync
    # ------------------------------------------------------------------

    async def sync_once(self) -> dict[str, Any]:
        """Run a single sync round against all authorised peers.

        Convenience wrapper that performs one iteration of the auto-sync
        discovery + sync logic **without** entering the periodic loop.
        Discovers visible peers, syncs with each authorised one, and
        returns a summary of the results.

        Returns
        -------
        dict with ``peers_synced`` (int) and ``results`` (list of per-peer
        summary dicts produced by :meth:`sync_with_peer`).
        """
        results: list[dict[str, Any]] = []
        try:
            peers = self.discovery.get_peers()
            for peer in peers:
                device_id = peer.get("device_id", "")
                if not device_id or not self.auth.is_authorized(device_id):
                    continue
                addr = (peer["ip"], peer["port"])
                logger.info(
                    "sync_once with %s (%s)",
                    peer.get("device_name", device_id),
                    addr,
                )
                summary = await self.sync_with_peer(addr)
                summary["peer"] = peer.get("device_name", device_id)
                results.append(summary)
        except Exception:
            logger.exception("sync_once iteration failed")
        return {"peers_synced": len(results), "results": results}

    # ------------------------------------------------------------------
    # Auto-sync
    # ------------------------------------------------------------------

    async def auto_sync(self, interval: float = 300.0) -> None:
        """Run periodic discovery + sync loop."""
        logger.info(
            "Auto-sync started (interval=%.1fs, device=%s)",
            interval,
            self.protocol.device_id,
        )
        while self._running:
            try:
                peers = self.discovery.get_peers()
                for peer in peers:
                    if not self.auth.is_authorized(peer["device_id"]):
                        continue
                    addr = (peer["ip"], peer["port"])
                    logger.info("Auto-syncing with %s (%s)", peer["device_name"], addr)
                    await self.sync_with_peer(addr)
            except Exception:
                logger.exception("Auto-sync iteration failed")
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Conflict resolution helper
    # ------------------------------------------------------------------

    def _resolve_conflict(
        self,
        conflict: Conflict,
        local_index: FileIndex,
        remote_index: FileIndex,
    ) -> dict[str, Any]:
        """Resolve a single conflict, consulting the callback and strategy."""
        # First ask the conflict callback
        if self._conflict_callback is not None:
            decision = self._conflict_callback(conflict)
            if decision is not None:
                return {"action": decision, "file_path": conflict.file_path}

        # Fall back to configured resolver or default to LWW
        if self._conflict_resolver is not None:
            return self._conflict_resolver.resolve(conflict, local_index, remote_index)

        return LastWriteWins().resolve(conflict, local_index, remote_index)

    # ------------------------------------------------------------------
    # Forward secrecy (ECDH session key)
    # ------------------------------------------------------------------

    def _establish_session_key(self, peer_public_key: bytes) -> bytes:
        """Derive an ephemeral session key via X25519 ECDH.

        Generates a fresh ephemeral key pair, performs Diffie-Hellman with
        *peer_public_key*, and derives a 32-byte session key via HKDF-SHA256.
        The session key replaces the long-term ``vault_key`` for transport
        encryption for the duration of this sync session, providing
        **forward secrecy**: even if the long-term key is later compromised,
        past sync traffic encrypted with the session key cannot be decrypted
        because the ephemeral private key is discarded.

        Parameters
        ----------
        peer_public_key:
            Raw 32-byte X25519 public key from the peer device.

        Returns
        -------
        The 32-byte session key (also stored in ``self._session_key``).

        If the ``cryptography`` library's X25519 or HKDF is unavailable, a
        warning is logged and the method falls back to the existing
        ``vault_key``, preserving compatibility.
        """
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric.x25519 import (
                X25519PrivateKey,
                X25519PublicKey,
            )
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF

            # Generate a fresh ephemeral key pair for this session.
            ephemeral_private = X25519PrivateKey.generate()
            peer_public = X25519PublicKey.from_public_bytes(peer_public_key)

            # ECDH shared secret
            shared_key = ephemeral_private.exchange(peer_public)

            # Derive a 32-byte session key via HKDF.
            hkdf = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=None,
                info=b"doctoragent-sync-session-v1",
            )
            session_key = hkdf.derive(shared_key)

            self._session_key = session_key
            logger.info("Forward-secret session key established via ECDH")
            return session_key

        except Exception as exc:
            logger.warning(
                "ECDH session key establishment failed (%s); "
                "falling back to long-term vault_key (no forward secrecy)",
                exc,
            )
            self._session_key = None
            return self.vault_key

    def get_session_public_key(self) -> bytes | None:
        """Generate an ephemeral key pair and return our public key.

        The public key is sent to the peer so they can call
        :meth:`_establish_session_key` with it.  The private key is kept
        in memory for the duration of the session.

        Returns ``None`` if X25519 is unavailable.
        """
        try:
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                PublicFormat,
            )

            self._ephemeral_private = X25519PrivateKey.generate()
            return self._ephemeral_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        except Exception as exc:
            logger.warning("Could not generate ephemeral key pair: %s", exc)
            return None

    def clear_session_key(self) -> None:
        """Discard the session key (call after sync completes).

        Once cleared, past sync traffic encrypted with the session key
        cannot be decrypted even if the long-term key is compromised.
        """
        self._session_key = None
        self._ephemeral_private = None

    # ------------------------------------------------------------------
    # Offline operation queue
    # ------------------------------------------------------------------

    def add_operation(
        self,
        op_type: str,
        file_path: str,
        content_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a file operation in the offline queue.

        Call this whenever a file is created, updated, or deleted while
        the device is offline.  On reconnection the queued operations
        are replayed to the peer.
        """
        return self._offline_queue.add_operation(op_type, file_path, content_hash, metadata)

    def get_pending_operations(self) -> list[dict[str, Any]]:
        """Return all pending offline operations."""
        return self._offline_queue.get_pending_operations()

    def clear_operations(self, count: int | None = None) -> int:
        """Clear operations from the offline queue.

        If *count* is ``None`` all operations are cleared; otherwise only
        the first *count* are removed.  Returns the number removed.
        """
        return self._offline_queue.clear_operations(count)

    def replay_offline_operations(self) -> list[dict[str, Any]]:
        """Return pending operations and populate SyncState.

        Bridges the offline queue with ``SyncState.pending_changes`` so
        that the existing state-persistence path carries the operation log
        to disk.  The operations remain in the queue until explicitly
        cleared via :meth:`clear_operations` (typically after the peer
        acknowledges receipt).
        """
        ops = self._offline_queue.get_pending_operations()
        self._offline_queue.replay_to_state(self._sync_state)
        self._save_state()
        return ops

    # ------------------------------------------------------------------
    # Sync progress and statistics
    # ------------------------------------------------------------------

    def get_sync_progress(self) -> dict[str, Any]:
        """Return the current sync progress snapshot."""
        return self._progress_reporter.get_progress()

    def get_sync_statistics(self) -> dict[str, Any]:
        """Return sync statistics (current + historical summary)."""
        return {
            "current": self._statistics.get_current(),
            "summary": self._statistics.get_summary(),
        }

    def get_sync_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent *limit* sync records."""
        return self._statistics.get_history(limit)

    # ------------------------------------------------------------------
    # SyncState persistence
    # ------------------------------------------------------------------

    def _encrypt_state(self, state: SyncState) -> bytes:
        """Serialize and encrypt SyncState."""
        raw = json.dumps(
            {
                "last_sync_time": state.last_sync_time,
                "known_files": state.known_files,
                "pending_changes": state.pending_changes,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return self.protocol.encrypt_transport(raw, self.vault_key)

    def _decrypt_state(self, data: bytes) -> SyncState:
        """Decrypt and deserialize SyncState."""
        plaintext = self.protocol.decrypt_transport(data, self.vault_key)
        obj = json.loads(plaintext.decode("utf-8"))
        return SyncState(
            last_sync_time=obj.get("last_sync_time", {}),
            known_files=obj.get("known_files", {}),
            pending_changes=obj.get("pending_changes", []),
        )

    def _save_state(self) -> None:
        """Persist SyncState to ``vault/.sync_state.json``.

        Before serialising, pending offline operations are synced into
        ``SyncState.pending_changes`` so the encrypted state file always
        reflects the latest operation log.
        """
        try:
            # Bridge the offline queue into SyncState.pending_changes so the
            # previously-unused field now carries real data.
            self._offline_queue.replay_to_state(self._sync_state)

            encrypted = self._encrypt_state(self._sync_state)
            tmp = self._state_path.with_suffix(".sync_state.json.tmp")
            tmp.write_bytes(encrypted)
            tmp.replace(self._state_path)
        except Exception:
            logger.exception("Failed to save sync state")

    def _load_state(self) -> None:
        """Load SyncState from ``vault/.sync_state.json``."""
        if not self._state_path.exists():
            return
        try:
            data = self._state_path.read_bytes()
            self._sync_state = self._decrypt_state(data)
        except Exception:
            logger.warning("Failed to load sync state, starting fresh")
            self._sync_state = SyncState()
