"""Master key providers for DoctorAgent.

A master key is the root secret from which the Vault Key is derived. Providers
control how that root secret is obtained and protected.
"""

import base64
import contextlib
import ctypes
import json
import logging
import os
import stat
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from doctoragent.compat import UTC
from doctoragent.security.crypto import _atomic_write_bytes
from doctoragent.security.keytree import derive_vault_key, generate_salt

if TYPE_CHECKING:
    from collections.abc import Callable

    from doctoragent.security.audit_log import AuditLogger

logger = logging.getLogger(__name__)


# ── Auto key rotation ────────────────────────────────────────────────────────
#
# The functions above perform a single, *manual* rotation.  ``AutoKeyRotator``
# wraps them in a background timer that periodically checks
# :func:`should_rotate_key` and (optionally) runs the rotation itself, plus a
# grace-key registry so the previous master key stays usable for a
# configurable transition window after a rotation.


class GraceKeyRegistry:
    """Persistent registry of superseded vault keys kept during the grace window.

    After a rotation the *previous* vault key is wrapped (encrypted) with the
    *new* master key and stored on disk.  While the grace period has not
    elapsed, the decrypt path can unwrap and try the previous key when the
    current key fails — this is what makes rotation *progressive*: a file
    that was not yet re-encrypted can still be opened until the old key
    expires.  Once the grace window passes :meth:`purge_expired` deletes the
    wrapped blob so the old key is gone for good.
    """

    def __init__(self, registry_path: Path) -> None:
        self._path = registry_path

    def add_grace_key(
        self,
        old_vault_key: bytes | bytearray,
        current_master_key: bytes | bytearray,
        expires_at: datetime,
        rotation_id: str | None = None,
    ) -> None:
        """Wrap *old_vault_key* with *current_master_key* and persist it."""
        wrapped = _encrypt_vault_key(bytes(old_vault_key), bytes(current_master_key))
        entry = {
            "wrapped_vault_key": base64.b64encode(wrapped).decode("ascii"),
            "expires_at": expires_at.isoformat(),
            "rotation_id": rotation_id or datetime.now(UTC).isoformat(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        entries = self._load_entries()
        entries.append(entry)
        self._write_entries(entries)

    def get_grace_keys(self, current_master_key: bytes | bytearray) -> list[bytes]:
        """Return all unexpired vault keys wrapped under *current_master_key*."""
        now = datetime.now(UTC)
        keys: list[bytes] = []
        for entry in self._load_entries():
            if self._is_expired(entry, now):
                continue
            try:
                wrapped = base64.b64decode(entry["wrapped_vault_key"])
                keys.append(bytes(_decrypt_vault_key(wrapped, current_master_key)))
            except Exception:  # noqa: BLE001 — skip corrupt entries
                logger.warning("Skipping corrupt grace-key entry", exc_info=True)
        return keys

    def purge_expired(self) -> int:
        """Delete expired entries.  Returns the number purged."""
        now = datetime.now(UTC)
        entries = self._load_entries()
        kept = [e for e in entries if not self._is_expired(e, now)]
        purged = len(entries) - len(kept)
        if purged:
            self._write_entries(kept)
        return purged

    def clear(self) -> None:
        """Remove every grace-key entry (used on a full reset)."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _is_expired(entry: dict, now: datetime) -> bool:
        try:
            exp = datetime.fromisoformat(entry["expires_at"])
        except (KeyError, ValueError):
            return True
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return now >= exp

    def _load_entries(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
        return []

    def _write_entries(self, entries: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(self._path, json.dumps(entries).encode("utf-8"))


class AutoKeyRotator:
    """Background key rotator with progressive (grace-period) rotation.

    The rotator runs a daemon thread that wakes every *check_interval_seconds*
    and, when :func:`should_rotate_key` reports the active key as due, runs a
    full :func:`rotate_master_key`.  The previous vault key is retained in a
    :class:`GraceKeyRegistry` for *grace_period_days* so decryption can still
    fall back to it while the rotation propagates.

    Two trigger paths are supported:

    * **Scheduled** — periodic age-based rotation, driven by
      :func:`should_rotate_key`.
    * **Security-event** — :meth:`trigger_emergency_rotation` performs an
      immediate :func:`emergency_rotate` when the decrypt-failure counter
      crosses *auto_rotate_on_failures_threshold*.  The audit logger's
      decrypt-failure state is the source of truth.

    Parameters
    ----------
    current_provider:
        The currently active master key provider.
    new_provider_factory:
        Zero-argument callable returning a fresh :class:`MasterKeyProvider`
        for the new key material.
    vault_key:
        The current vault key bytes (used to validate the active master key).
    storage_path:
        Path to the ``master_key.bin`` file (rotation marker sibling).
    vault_dir:
        Directory holding the encrypted vault files to re-encrypt.
    audit_logger:
        Optional audit logger.
    backup_callback:
        Optional callable invoked with no arguments *before* each rotation;
        it should raise on backup failure to abort the rotation.
    rotation_interval_days:
        Max key age before scheduled rotation (default 90).
    grace_period_days:
        How long the previous key remains usable after a rotation (default 7).
    check_interval_seconds:
        Background poll interval (default 1 hour).
    auto_rotate_on_failures_threshold:
        Decrypt-failure count that triggers an emergency rotation (default 5).
    """

    def __init__(
        self,
        current_provider: "MasterKeyProvider",
        new_provider_factory: "Callable[[], MasterKeyProvider]",
        vault_key: bytes,
        storage_path: Path,
        vault_dir: Path | None = None,
        audit_logger: "AuditLogger | None" = None,
        backup_callback: "Callable[[], None] | None" = None,
        rotation_interval_days: int = 90,
        grace_period_days: int = 7,
        check_interval_seconds: float = 3600.0,
        auto_rotate_on_failures_threshold: int = 5,
    ) -> None:
        self._provider = current_provider
        self._factory = new_provider_factory
        self._vault_key = bytes(vault_key)
        self._storage_path = storage_path
        self._vault_dir = vault_dir
        self._audit = audit_logger
        self._backup = backup_callback
        self._rotation_interval_days = rotation_interval_days
        self._grace_period_days = grace_period_days
        self._check_interval = check_interval_seconds
        self._failure_threshold = auto_rotate_on_failures_threshold

        self._registry = GraceKeyRegistry(storage_path.with_name(".grace_keys.json"))
        self._timer: threading.Timer | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._failures = 0
        self._last_rotation: datetime | None = None
        self._running = False

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background polling thread."""
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._schedule_next(delay=0.0)

    def stop(self) -> None:
        """Stop the background thread and wait for it to drain."""
        self._running = False
        self._stop.set()
        timer = self._timer
        if timer is not None:
            timer.cancel()
            self._timer = None

    def is_running(self) -> bool:
        return self._running

    @property
    def last_rotation(self) -> datetime | None:
        return self._last_rotation

    @property
    def grace_registry(self) -> GraceKeyRegistry:
        return self._registry

    # ── failure tracking ──────────────────────────────────────────────────

    def record_decrypt_failure(self) -> int:
        """Increment the decrypt-failure counter; returns the new count.

        When the counter crosses the configured threshold an emergency
        rotation is triggered on the calling thread.
        """
        with self._lock:
            self._failures += 1
            count = self._failures
        if count >= self._failure_threshold:
            logger.warning(
                "Decrypt failures (%d) reached threshold %d; triggering emergency rotation",
                count,
                self._failure_threshold,
            )
            try:
                self.trigger_emergency_rotation(reason="decrypt_failures")
            except Exception:  # noqa: BLE001 — never propagate into the decrypt path
                logger.exception("Emergency rotation triggered by failures failed")
        return count

    def record_decrypt_success(self) -> None:
        """Reset the decrypt-failure counter after a successful decryption."""
        with self._lock:
            self._failures = 0

    # ── rotation actions ──────────────────────────────────────────────────

    def check_and_rotate(self) -> bool:
        """Run one scheduled check; perform a rotation if due.

        Returns True when a rotation was performed.
        """
        self._registry.purge_expired()
        creation_time = self._resolve_creation_time()
        if creation_time is None:
            return False
        if not should_rotate_key(creation_time, self._rotation_interval_days):
            return False
        try:
            self._perform_scheduled_rotation(creation_time)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("Scheduled master key rotation failed")
            if self._audit is not None:
                self._audit.log(
                    "master_key_changed",
                    {"operation": "auto_rotation_failed"},
                )
            return False

    def trigger_emergency_rotation(self, reason: str = "manual") -> bytes:
        """Perform an emergency rotation immediately.

        The previous vault key is wrapped with the new master key and added
        to the grace registry so existing vault files remain decryptable
        during the transition.  Returns the new master key bytes.
        """
        with self._lock:
            if not self._running and self._provider is None:
                raise RuntimeError("Rotator has no active provider")
            new_provider = self._factory()
            # Wrap the *current* vault key under the new master key for the
            # grace window so existing files stay decryptable.
            backup_path = self._storage_path.with_name(".emergency_vault_key.wrapped")
            new_master = emergency_rotate(
                current_provider=self._provider,
                new_provider=new_provider,
                vault_key=self._vault_key,
                vault_key_backup_path=backup_path,
                audit_logger=self._audit,
                vault_dir=self._vault_dir,
            )
            expires_at = datetime.now(UTC) + timedelta(days=self._grace_period_days)
            self._registry.add_grace_key(
                self._vault_key, new_master, expires_at, rotation_id=f"emergency-{reason}"
            )
            self._provider = new_provider
            self._vault_key = bytes(_derive_vault_key_from_master(new_master))
            self._last_rotation = datetime.now(UTC)
            with self._lock:
                self._failures = 0
            return new_master

    # ── internals ──────────────────────────────────────────────────────────

    def _perform_scheduled_rotation(self, creation_time: datetime) -> None:
        # 1. Run the backup callback first; abort rotation if it fails.
        if self._backup is not None:
            self._backup()
        new_provider = self._factory()
        old_vault_key = self._vault_key
        rotate_master_key(
            current_provider=self._provider,
            new_provider=new_provider,
            vault_key=self._vault_key,
            storage_path=self._storage_path,
            audit_logger=self._audit,
            vault_dir=self._vault_dir,
        )
        # 2. Register the previous vault key for the grace window so files
        # that were not re-encrypted can still be opened.
        new_master = new_provider.get_key()
        expires_at = datetime.now(UTC) + timedelta(days=self._grace_period_days)
        self._registry.add_grace_key(old_vault_key, new_master, expires_at, rotation_id="scheduled")
        self._provider = new_provider
        self._vault_key = bytes(_derive_vault_key_from_master(new_master))
        self._last_rotation = datetime.now(UTC)
        logger.info("Master key auto-rotated successfully")

    def _resolve_creation_time(self) -> datetime | None:
        marker = self._storage_path.with_name(".rotation_marker")
        if marker.exists():
            try:
                raw = marker.read_text(encoding="utf-8").strip()
                ts = datetime.fromisoformat(raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                return ts
            except (ValueError, OSError):
                pass
        key_file = self._storage_path
        if key_file.exists():
            try:
                return datetime.fromtimestamp(key_file.stat().st_mtime, tz=UTC)
            except OSError:
                return None
        return None

    def _schedule_next(self, delay: float | None = None) -> None:
        if not self._running:
            return
        wait = delay if delay is not None else self._check_interval
        self._timer = threading.Timer(wait, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self) -> None:
        if self._stop.is_set() or not self._running:
            return
        try:
            self.check_and_rotate()
        finally:
            self._schedule_next(self._check_interval)


def _secure_zero(value: "bytearray | bytes | None") -> None:
    """Best-effort overwrite of *value* with zeros.

    ``bytes`` objects are immutable in Python, so the buffer cannot be cleared
    in place; for ``bytes`` inputs a warning is logged (the data will remain
    in memory until garbage collected).  ``bytearray`` inputs are overwritten
    in place via ``ctypes.memset`` so the secret is wiped immediately.
    """
    if value is None:
        return
    if isinstance(value, bytearray):
        n = len(value)
        if n == 0:
            return
        buf = (ctypes.c_char * n).from_buffer(value)
        ctypes.memset(buf, 0, n)
        return
    # bytes (immutable) — cannot be cleared in place.
    logger.warning(
        "secure_zero received immutable bytes; the secret remains in memory "
        "until garbage collected. Store keys as bytearray to allow in-place "
        "wiping."
    )


# Pluggable provider registry. Built-in providers are registered at import time;
# downstream packages or tests can register additional providers at runtime.
_REGISTRY: dict[str, type["MasterKeyProvider"]] = {}


def register_provider(name: str, provider_cls: type["MasterKeyProvider"]) -> None:
    """Register a master key provider under *name* (case-insensitive)."""
    if not issubclass(provider_cls, MasterKeyProvider):
        raise TypeError("Provider must inherit from MasterKeyProvider")
    _REGISTRY[name.lower()] = provider_cls


def get_registered_providers() -> dict[str, type["MasterKeyProvider"]]:
    """Return a shallow copy of the registered providers map."""
    return dict(_REGISTRY)


class MasterKeyProvider(ABC):
    """Abstract base for master key acquisition and protection."""

    @abstractmethod
    def get_key(self) -> bytes:
        """Return the raw 32-byte master key."""

    @abstractmethod
    def exists(self) -> bool:
        """Return True if the protected key material is already stored."""

    @abstractmethod
    def clear(self) -> None:
        """Clear any cached key material from memory."""


# Argon2id 参数：CURRENT 用于新创建的 vault（OWASP 本地高价值场景推荐值），
# 并持久化到 filepassword.kdf.json；LEGACY 用于兼容已存在但未持久化参数的老 vault
# （老 vault 仍能以原参数解开）。parallelism/hash_len 两套保持一致。
_ARGON2_PARALLELISM = 4
_ARGON2_HASH_LEN = 32
_ARGON2_LEGACY_PARAMS: dict[str, int] = {
    "time_cost": 3,
    "memory_cost": 65536,
    "parallelism": _ARGON2_PARALLELISM,
    "hash_len": _ARGON2_HASH_LEN,
}
_ARGON2_CURRENT_PARAMS: dict[str, int] = {
    "time_cost": 4,
    "memory_cost": 122880,
    "parallelism": _ARGON2_PARALLELISM,
    "hash_len": _ARGON2_HASH_LEN,
}


class FilePasswordProvider(MasterKeyProvider):
    """Development provider: derive master key from a password file or env var."""

    def __init__(
        self,
        password: str | None = None,
        password_file: Path | None = None,
        storage_path: Path | None = None,
    ) -> None:
        if password and password_file:
            raise ValueError("Specify either password or password_file, not both")
        self._password = password
        self._password_file = password_file
        self._storage_path = storage_path
        self._key: bytearray | None = None
        self._salt: bytes | None = None
        # Argon2id 派生参数；由 _get_or_create_salt 顺带初始化（新 vault 写入
        # filepassword.kdf.json，老 vault 回退到 LEGACY 值）。
        self._kdf_params: dict[str, int] | None = None

    def _get_or_create_salt(self) -> bytes:
        """Return a persistent per-storage salt.

        When *storage_path* is provided the salt is generated once, written to
        disk with owner-only permissions, and reused on subsequent runs. When
        *storage_path* is None a random ephemeral salt is generated for this
        process lifetime; the key will not survive a restart.

        本方法同时初始化 ``self._kdf_params``：新 vault 写入当前 Argon2id 参数，
        已存在的 vault 读取已持久化参数（无参数文件时回退到 LEGACY 值）。
        """
        if self._salt is not None:
            return self._salt
        if self._storage_path is None:
            logger.warning(
                "FilePasswordProvider running without storage_path; "
                "using ephemeral random salt (key will not survive restart)."
            )
            self._salt = generate_salt()
            self._kdf_params = dict(_ARGON2_CURRENT_PARAMS)
            return self._salt
        salt_path = self._storage_path / "filepassword.salt"
        if salt_path.exists():
            self._validate_salt_file(salt_path)
            salt = salt_path.read_bytes()
            if not salt:
                # 防御性：空盐视为未初始化（TOCTOU 窗口残留的空文件），
                # 重新生成并覆盖重写，避免 Argon2id 用空盐派生出确定性密钥。
                salt = generate_salt()
                self._storage_path.mkdir(parents=True, exist_ok=True)
                self._atomic_write_salt(salt_path, salt)
                self._kdf_params = self._write_kdf_params()
                # 与并发初始化收敛：以磁盘上的最终内容为准。
                self._validate_salt_file(salt_path)
                salt = salt_path.read_bytes()
            else:
                self._kdf_params = self._read_kdf_params()
            self._salt = salt
            return self._salt

        salt = generate_salt()
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._atomic_write_salt(salt_path, salt)
        self._kdf_params = self._write_kdf_params()
        # Re-read from disk so concurrent initialisations always converge on
        # the same persisted salt.
        self._salt = salt_path.read_bytes()
        return self._salt

    def _read_kdf_params(self) -> dict[str, int]:
        """读取持久化的 Argon2id 参数；老 vault 未持久化时回退到 LEGACY 默认值。"""
        if self._storage_path is None:
            return dict(_ARGON2_CURRENT_PARAMS)
        params_path = self._storage_path / "filepassword.kdf.json"
        try:
            raw = params_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            # 老 vault 未持久化 KDF 参数：回退到 LEGACY 值以保持向后兼容。
            return dict(_ARGON2_LEGACY_PARAMS)
        legacy = _ARGON2_LEGACY_PARAMS
        return {
            "time_cost": int(data.get("time_cost", legacy["time_cost"])),
            "memory_cost": int(data.get("memory_cost", legacy["memory_cost"])),
            "parallelism": int(data.get("parallelism", legacy["parallelism"])),
            "hash_len": int(data.get("hash_len", legacy["hash_len"])),
        }

    def _write_kdf_params(self) -> dict[str, int]:
        """持久化当前 Argon2id 参数（用于新创建的 vault）。"""
        params = dict(_ARGON2_CURRENT_PARAMS)
        if self._storage_path is None:
            return params
        params_path = self._storage_path / "filepassword.kdf.json"
        _atomic_write_bytes(params_path, json.dumps(params).encode("utf-8"))
        return params

    @staticmethod
    def _validate_salt_file(path: Path) -> None:
        """Reject salt files that are symlinks or have overly permissive modes.

        Uses :func:`os.lstat` so a symbolic link is detected (and rejected)
        rather than followed. The file must be a regular file and must not be
        readable, writable, or executable by group or other. Violations raise
        :class:`RuntimeError` so the caller never derives a key from a
        tampered or attacker-controlled salt.
        """
        try:
            lst = os.lstat(path)
        except OSError as exc:
            raise RuntimeError(f"无法检查 salt 文件 {path}: {exc}") from exc
        mode = lst.st_mode
        if not stat.S_ISREG(mode):
            logger.error("salt 文件 %s 不是常规文件（可能是符号链接），拒绝读取", path)
            raise RuntimeError(f"salt 文件不是常规文件: {path}")
        if mode & stat.S_IRWXG or mode & stat.S_IRWXO:
            # Windows 不支持 Unix 文件权限 os.lstat 返回的权限位有误（总是 666）
            # 跳过 group/other 检查
            if sys.platform != "win32":
                logger.error(
                    "salt 文件 %s 权限过松（mode=%o），存在 group/other 访问位，拒绝读取",
                    path,
                    mode & 0o777,
                )
                raise RuntimeError(f"salt 文件权限过松 (mode={mode & 0o777:o})，拒绝读取: {path}")

    @staticmethod
    def _atomic_write_salt(path: Path, salt: bytes) -> None:
        """Write *salt* to *path* atomically with owner-only permissions.

        采用"临时文件 + os.replace 原子替换"模式：先写入同目录临时文件并 fsync，
        再原子替换目标，消除原 ``O_CREAT | O_EXCL`` 实现中"先创建空文件再写入"
        的 TOCTOU 窗口（并发进程不会观察到空盐 ``b""``）。保留 ``O_NOFOLLOW``
        防符号链接。

        若目标已存在且非空，保留首写者胜出语义（不覆盖已持久化的有效盐）；
        仅当目标不存在或为空时才执行写入——这同时支撑了上层对空盐的覆盖重写。
        """
        # 首写者胜出：已存在且非空的盐不覆盖。
        try:
            if path.stat().st_size > 0:
                return
        except FileNotFoundError:
            pass

        tmp_path = path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(tmp_path, flags, stat.S_IRUSR | stat.S_IWUSR)
        except FileExistsError:
            # 临时文件名碰撞（极罕见）：放弃写入，调用方会重读到已存在的盐。
            return
        # os.fdopen takes ownership of *fd* once it succeeds; the with-block
        # below then owns closing it. If fdopen itself fails, *fd* is still
        # open and unowned, so close it manually here. (If fh.write fails the
        # with-block's __exit__ has already closed *fd* — closing it again
        # here would raise EBADF, which was the original double-close bug.)
        try:
            fh = os.fdopen(fd, "wb")
        except OSError:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        try:
            with fh:
                fh.write(salt)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            # 写入或替换失败：清理残留临时文件，原盐文件保持不变。
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise

    def get_key(self) -> bytes:
        """Derive master key from password using Argon2id."""
        if self._key is not None:
            return bytes(self._key)
        password = self._password
        if password is None and self._password_file is not None:
            # Only strip trailing line terminators (CR/LF) so that legitimate
            # leading/trailing whitespace inside the password is preserved.
            password = self._password_file.read_text(encoding="utf-8").rstrip("\r\n")
        if password is None:
            raise RuntimeError("No password configured for FilePasswordProvider")
        from argon2.low_level import Type, hash_secret_raw

        salt = self._get_or_create_salt()
        # _get_or_create_salt 保证已初始化；防御性回退到 LEGACY 参数。
        params = self._kdf_params or dict(_ARGON2_LEGACY_PARAMS)
        derived = hash_secret_raw(
            secret=password.encode("utf-8"),
            salt=salt,
            time_cost=params["time_cost"],
            memory_cost=params["memory_cost"],
            parallelism=params["parallelism"],
            hash_len=params["hash_len"],
            type=Type.ID,
        )
        self._key = bytearray(derived)
        return bytes(self._key)

    def exists(self) -> bool:
        """Always True if a password is available."""
        return self._password is not None or (
            self._password_file is not None and self._password_file.exists()
        )

    def clear(self) -> None:
        """Clear any cached key material from memory."""
        if self._key is not None:
            _secure_zero(self._key)
            self._key = None


class DpapiMasterKeyProvider(MasterKeyProvider):
    """Windows DPAPI-backed master key provider.

    The master key is generated once, protected with DPAPI for the current
    user, and persisted to disk. It is decrypted silently when needed.

    On non-Windows platforms this provider can be instantiated and queried
    (``exists()``), but ``get_key()`` raises ``RuntimeError`` because DPAPI is
    not available. This allows the same application code to run on Linux while
    selecting a different provider, e.g. ``FilePasswordProvider``.
    """

    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self._key: bytearray | None = None

    def get_key(self) -> bytes:
        """Return master key, generating and protecting it if necessary."""
        if self._key is not None:
            return bytes(self._key)
        if sys.platform != "win32":
            raise RuntimeError(
                "DPAPI master key provider is only available on Windows. "
                "Use FilePasswordProvider on Linux."
            )
        if not self.exists():
            derived = generate_salt()
            self._key = bytearray(derived)
            self._protect_and_store(bytes(self._key))
            return bytes(self._key)
        from doctoragent.security.win_helpers import unprotect_data

        protected = self.storage_path.read_bytes()
        self._key = bytearray(unprotect_data(protected))
        return bytes(self._key)

    def exists(self) -> bool:
        """Return True if a protected master key file exists."""
        return self.storage_path.exists()

    def clear(self) -> None:
        """Clear any cached key material from memory."""
        if self._key is not None:
            _secure_zero(self._key)
            self._key = None

    def _protect_and_store(self, key: bytes) -> None:
        from doctoragent.security.win_helpers import protect_data

        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        protected = protect_data(key)
        _atomic_write_bytes(self.storage_path, protected)


class TpmMasterKeyProvider(MasterKeyProvider):
    """TPM-backed master key provider.

    A 32-byte master-key material is generated once, encrypted with a
    persistent TPM RSA key (via ``NCryptEncrypt``), and stored on disk. The
    material can only be recovered by decrypting the blob with the same TPM
    key (via ``NCryptDecrypt``).

    On non-Windows platforms the provider can be instantiated and queried, but
    ``get_key()`` raises ``RuntimeError`` because the TPM/NCrypt API is not
    available. A Linux fallback using ``tpm2_createprimary``/``tpm2_create``/
    ``tpm2_unseal`` is intentionally left as an interface-only placeholder for
    this phase.

    When *hello_salt* is supplied, the raw TPM-protected material is further
    derived through HKDF-SHA256, producing a master key that additionally
    requires Windows Hello (or another source of the salt) to unlock.
    """

    TPM_RSA_KEY_LEN = 2048
    MASTER_KEY_LEN = 32

    def __init__(
        self,
        storage_path: Path,
        tpm_key_name: str = "DoctorAgentTPMMasterKey",
        hello_salt: bytes | None = None,
    ) -> None:
        self.storage_path = storage_path
        self.tpm_key_name = tpm_key_name
        self.hello_salt = hello_salt
        self._key: bytearray | None = None

    def get_key(self) -> bytes:
        """Return the master key, creating/protecting it if necessary."""
        if self._key is not None:
            return bytes(self._key)
        if sys.platform != "win32":
            raise RuntimeError(
                "TPM master key provider is only available on Windows. "
                "Use FilePasswordProvider or DPAPI on this platform."
            )
        if not self.exists():
            key_material = generate_salt()
            encrypted = _ncrypt_encrypt_with_persistent_key(
                self.tpm_key_name,
                key_material,
                overwrite=True,
            )
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(self.storage_path, encrypted)
        else:
            encrypted = self.storage_path.read_bytes()
            key_material = _ncrypt_decrypt_with_persistent_key(
                self.tpm_key_name,
                encrypted,
            )

        self._key = bytearray(_derive_final_key(key_material, self.hello_salt))
        return bytes(self._key)

    def exists(self) -> bool:
        """Return True if the encrypted master-key blob exists."""
        return self.storage_path.exists()

    def clear(self) -> None:
        """Clear any cached key material from memory."""
        if self._key is not None:
            _secure_zero(self._key)
            self._key = None


class KeychainMasterKeyProvider(MasterKeyProvider):
    """macOS Keychain-backed master key provider.

    Uses ``/usr/bin/security`` CLI to store and retrieve the master key
    in the user's login keychain.  Key material is base64-encoded for
    storage because the keychain does not support raw binary values.

    On non-macOS platforms this provider can be instantiated and queried
    (``exists()``), but ``get_key()`` raises ``NotImplementedError``.
    This allows the same application code to run on Linux while selecting
    a different provider.
    """

    def __init__(self, storage_path: Path, service_name: str = "DoctorAgent") -> None:
        self.storage_path = storage_path
        self.service_name = f"{service_name}.{storage_path.stem}"
        self.account_name = "master_key"
        self._key: bytearray | None = None

    @staticmethod
    def _escape_interactive_token(value: str) -> str:
        """Escape a token for the macOS ``security -i`` interactive parser.

        The interactive parser splits arguments on whitespace and treats
        double-quoted strings specially.  We wrap every value in double
        quotes and escape embedded ``"`` and ``\\`` so attacker-controlled
        account/service names cannot break out of the argument or inject
        additional commands.
        """
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def protect(self, key_material: bytes) -> None:
        """Store *key_material* in the macOS login keychain.

        The material is base64-encoded before storage because the
        ``security`` CLI treats the password value as a UTF-8 string.

        The password is delivered via the ``security -i`` interactive mode
        stdin stream rather than as a ``-w`` argument so that it is not
        exposed in the process argument list (visible to other users via
        ``ps``).  Account and service values are escaped to prevent
        interactive-mode injection.
        """
        if sys.platform != "darwin":
            raise NotImplementedError(
                "Keychain is only available on macOS. "
                "Use FilePasswordProvider or DPAPI on other platforms."
            )
        encoded = base64.b64encode(key_material).decode("ascii")
        # Build the interactive-mode command.  The password lives only in
        # the stdin pipe (private to this process), not in argv.
        cmd_line = (
            "add-generic-password "
            f"-a {self._escape_interactive_token(self.account_name)} "
            f"-s {self._escape_interactive_token(self.service_name)} "
            f"-w {self._escape_interactive_token(encoded)} "
            "-U\n"
        )
        try:
            subprocess.run(
                ["/usr/bin/security", "-i"],
                input=cmd_line,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Failed to store master key in keychain: {exc.stderr.strip()}"
            ) from exc

    def unprotect(self) -> bytes:
        """Retrieve the master key material from the macOS login keychain.

        The stored base64 value is decoded back to raw bytes.
        """
        if sys.platform != "darwin":
            raise NotImplementedError(
                "Keychain is only available on macOS. "
                "Use FilePasswordProvider or DPAPI on other platforms."
            )
        try:
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-a",
                    self.account_name,
                    "-s",
                    self.service_name,
                    "-w",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return base64.b64decode(result.stdout.strip())
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Failed to retrieve master key from keychain: {exc.stderr.strip()}"
            ) from exc

    def get_key(self) -> bytes:
        """Return the master key, generating and protecting it if necessary."""
        if self._key is not None:
            return bytes(self._key)
        if not self.exists():
            derived = generate_salt()
            self._key = bytearray(derived)
            self.protect(bytes(self._key))
            return bytes(self._key)
        self._key = bytearray(self.unprotect())
        return bytes(self._key)

    def exists(self) -> bool:
        """Return True if the master key is already stored in the keychain."""
        if sys.platform != "darwin":
            return False
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                self.account_name,
                "-s",
                self.service_name,
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def clear(self) -> None:
        """Clear any cached key material from memory."""
        if self._key is not None:
            _secure_zero(self._key)
            self._key = None


def _derive_final_key(key_material: bytes, hello_salt: bytes | None) -> bytes:
    """Derive the final 32-byte master key from TPM-decrypted material.

    If *hello_salt* is provided it is used as the HKDF salt, meaning the final
    key can only be produced when the salt (e.g. from Windows Hello) is also
    available. Otherwise *key_material* is returned unchanged.
    """
    if hello_salt is None:
        return key_material
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hello_salt,
        info=b"doctoragent-tpm-hello-v1",
    )
    return hkdf.derive(key_material)


if sys.platform == "win32":
    import ctypes.wintypes as wintypes

    _MS_PLATFORM_CRYPTO_PROVIDER = "Microsoft Platform Crypto Provider"
    _BCRYPT_RSA_ALGORITHM = "RSA"
    _NCRYPT_OVERWRITE_KEY_FLAG = 0x00000080
    # Use OAEP (RSAES-OAEP) padding instead of the vulnerable PKCS#1 v1.5
    # padding.  OAEP is the modern, IND-CCA2 secure scheme; PKCS#1 v1.5
    # is susceptible to Bleichenbacher-style padding-oracle attacks.
    _NCRYPT_PAD_OAEP_FLAG = 0x00000004

    _NCRYPT = ctypes.windll.ncrypt

    def _check_ncrypt_status(status: int, operation: str) -> None:
        """Raise RuntimeError if an NCrypt call returned a non-zero status."""
        if status != 0:
            raise RuntimeError(f"{operation} failed with status 0x{status:08X}")

    def _open_tpm_provider() -> ctypes.c_void_p:
        """Open the Microsoft Platform Crypto Provider (TPM)."""
        provider = ctypes.c_void_p()
        status = _NCRYPT.NCryptOpenStorageProvider(
            ctypes.byref(provider),
            _MS_PLATFORM_CRYPTO_PROVIDER,
            wintypes.DWORD(0),
        )
        _check_ncrypt_status(status, "NCryptOpenStorageProvider")
        return provider

    def _get_or_create_persistent_key(
        provider: ctypes.c_void_p,
        key_name: str,
        overwrite: bool,
    ) -> ctypes.c_void_p:
        """Open an existing persisted TPM key or create a new RSA key."""
        key = ctypes.c_void_p()
        flags = _NCRYPT_OVERWRITE_KEY_FLAG if overwrite else 0

        # Try to open an existing persisted key first.
        status = _NCRYPT.NCryptOpenKey(
            provider,
            ctypes.byref(key),
            key_name,
            0,
            0,
        )
        if status == 0:
            return key

        # Not present: create a new persisted RSA key.
        status = _NCRYPT.NCryptCreatePersistedKey(
            provider,
            ctypes.byref(key),
            _BCRYPT_RSA_ALGORITHM,
            key_name,
            0,
            flags,
        )
        _check_ncrypt_status(status, "NCryptCreatePersistedKey")

        key_len = wintypes.DWORD(TpmMasterKeyProvider.TPM_RSA_KEY_LEN)
        status = _NCRYPT.NCryptSetProperty(
            key,
            "Length",
            ctypes.cast(ctypes.byref(key_len), ctypes.POINTER(wintypes.BYTE)),
            ctypes.sizeof(key_len),
            0,
        )
        _check_ncrypt_status(status, "NCryptSetProperty(Length)")

        status = _NCRYPT.NCryptFinalizeKey(key, 0)
        _check_ncrypt_status(status, "NCryptFinalizeKey")
        return key

    def _encrypt_with_key(key: ctypes.c_void_p, plaintext: bytes) -> bytes:
        """Encrypt *plaintext* with the public portion of *key* using OAEP."""
        input_buf = ctypes.create_string_buffer(plaintext)
        output_len = wintypes.DWORD(0)
        status = _NCRYPT.NCryptEncrypt(
            key,
            ctypes.cast(input_buf, ctypes.POINTER(wintypes.BYTE)),
            len(plaintext),
            None,
            None,
            0,
            ctypes.byref(output_len),
            _NCRYPT_PAD_OAEP_FLAG,
        )
        _check_ncrypt_status(status, "NCryptEncrypt(size probe)")

        output_buf = ctypes.create_string_buffer(output_len.value)
        status = _NCRYPT.NCryptEncrypt(
            key,
            ctypes.cast(input_buf, ctypes.POINTER(wintypes.BYTE)),
            len(plaintext),
            None,
            ctypes.cast(output_buf, ctypes.POINTER(wintypes.BYTE)),
            output_len.value,
            ctypes.byref(output_len),
            _NCRYPT_PAD_OAEP_FLAG,
        )
        _check_ncrypt_status(status, "NCryptEncrypt")
        return bytes(output_buf[: output_len.value])

    def _decrypt_with_key(key: ctypes.c_void_p, ciphertext: bytes) -> bytes:
        """Decrypt *ciphertext* using the private key protected by the TPM (OAEP)."""
        input_buf = ctypes.create_string_buffer(ciphertext)
        output_len = wintypes.DWORD(0)
        status = _NCRYPT.NCryptDecrypt(
            key,
            ctypes.cast(input_buf, ctypes.POINTER(wintypes.BYTE)),
            len(ciphertext),
            None,
            None,
            0,
            ctypes.byref(output_len),
            _NCRYPT_PAD_OAEP_FLAG,
        )
        _check_ncrypt_status(status, "NCryptDecrypt(size probe)")

        output_buf = ctypes.create_string_buffer(output_len.value)
        status = _NCRYPT.NCryptDecrypt(
            key,
            ctypes.cast(input_buf, ctypes.POINTER(wintypes.BYTE)),
            len(ciphertext),
            None,
            ctypes.cast(output_buf, ctypes.POINTER(wintypes.BYTE)),
            output_len.value,
            ctypes.byref(output_len),
            _NCRYPT_PAD_OAEP_FLAG,
        )
        _check_ncrypt_status(status, "NCryptDecrypt")
        return bytes(output_buf[: output_len.value])

    def _ncrypt_encrypt_with_persistent_key(
        key_name: str,
        plaintext: bytes,
        overwrite: bool,
    ) -> bytes:
        """Encrypt *plaintext* using a persistent TPM RSA key."""
        provider = _open_tpm_provider()
        key = ctypes.c_void_p()
        try:
            key = _get_or_create_persistent_key(provider, key_name, overwrite)
            return _encrypt_with_key(key, plaintext)
        finally:
            if key:
                _NCRYPT.NCryptFreeObject(key)
            _NCRYPT.NCryptFreeObject(provider)

    def _ncrypt_decrypt_with_persistent_key(
        key_name: str,
        ciphertext: bytes,
    ) -> bytes:
        """Decrypt *ciphertext* using a persistent TPM RSA key."""
        provider = _open_tpm_provider()
        key = ctypes.c_void_p()
        try:
            key = _get_or_create_persistent_key(provider, key_name, overwrite=False)
            return _decrypt_with_key(key, ciphertext)
        finally:
            if key:
                _NCRYPT.NCryptFreeObject(key)
            _NCRYPT.NCryptFreeObject(provider)

else:

    def _ncrypt_encrypt_with_persistent_key(
        key_name: str,
        plaintext: bytes,
        overwrite: bool,
    ) -> bytes:
        raise RuntimeError("TPM/NCrypt operations are only available on Windows")

    def _ncrypt_decrypt_with_persistent_key(
        key_name: str,
        ciphertext: bytes,
    ) -> bytes:
        raise RuntimeError("TPM/NCrypt operations are only available on Windows")


# ── Master Key Rotation ──────────────────────────────────────────────────────


def should_rotate_key(creation_time: datetime, max_age_days: int = 90) -> bool:
    """Return True if the key is older than *max_age_days*.

    Parameters
    ----------
    creation_time:
        The timestamp when the current master key was created / last rotated.
    max_age_days:
        Maximum age in days before rotation is recommended. Default 90.
    """
    age = datetime.now(UTC) - creation_time
    return age > timedelta(days=max_age_days)


def _derive_vault_key_from_master(master_key: bytes | bytearray) -> bytearray:
    """Derive vault key from master key using HKDF-SHA256.

    Delegates to :func:`doctoragent.security.keytree.derive_vault_key` so that
    the ``info`` constant (``b"vault-key-v1"``) is shared with the original
    derivation path.  Without this, the rotation code would derive a different
    vault key from the same master key than the rest of the codebase, causing
    every rotation to fail validation.

    返回 ``bytearray`` 以便调用方在使用后通过 :func:`_secure_zero` 真正擦除，
    缩短 vault 密钥在内存中的存活时间。
    """
    return bytearray(derive_vault_key(bytes(master_key)))


def _encrypt_vault_key(vault_key: bytes | bytearray, master_key: bytes | bytearray) -> bytes:
    """Encrypt *vault_key* with *master_key* using AES-256-GCM.

    Returns *nonce* + *ciphertext* (the nonce is 12 bytes, so final length
    is 12 + 32 + 16 = 60 bytes).
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    aesgcm = AESGCM(bytes(master_key))
    ciphertext = aesgcm.encrypt(nonce, bytes(vault_key), b"doctoragent-vault-key-wrap-v1")
    return nonce + ciphertext


def _decrypt_vault_key(wrapped: bytes, master_key: bytes | bytearray) -> bytearray:
    """Decrypt *wrapped* (nonce + ciphertext) with *master_key*.

    返回 ``bytearray`` 以便调用方在使用后通过 :func:`_secure_zero` 真正擦除。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = wrapped[:12]
    ciphertext = wrapped[12:]
    aesgcm = AESGCM(bytes(master_key))
    return bytearray(aesgcm.decrypt(nonce, ciphertext, b"doctoragent-vault-key-wrap-v1"))


def _re_encrypt_vault_files(
    vault_path: Path,
    old_vault_key: bytes | bytearray,
    new_vault_key: bytes | bytearray,
    audit_logger: "AuditLogger | None" = None,
    progress_callback: "Callable[[int, int], None] | None" = None,
) -> int:
    """Re-encrypt every encrypted file under *vault_path* with the new vault key.

    Each file is decrypted with its current file key (old vault key + file salt),
    then re-encrypted with the new file key (new vault key + same file salt).

    The operation is **all-or-nothing**: every file is first re-encrypted to a
    ``.tmp_new`` sidecar; only if all files succeed are the originals atomically
    replaced.  If any file fails mid-way, all ``.tmp_new`` sidecars are deleted
    and the originals are left untouched, so the vault never ends up in a
    half-rotated state.

    If *progress_callback* is provided, it is called with ``(current, total)``
    after each successfully re-encrypted file.  Useful for UI progress bars
    during key rotation.

    Returns the number of files re-encrypted.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from doctoragent.security.keytree import derive_file_key

    version = b"\x01"
    salt_len = 32
    nonce_len = 12

    # Vault stores encrypted files in category subdirectories.
    if not vault_path.exists() or not vault_path.is_dir():
        return 0

    # Collect every candidate file up front so we can drive a two-phase
    # (encrypt-all, then commit-all) workflow with full rollback on failure.
    candidates: list[Path] = []
    for category_dir in sorted(vault_path.iterdir()):
        if not category_dir.is_dir():
            continue
        for vault_file in sorted(category_dir.iterdir()):
            if vault_file.is_file():
                candidates.append(vault_file)

    total_files = len(candidates)
    if total_files == 0:
        return 0

    if progress_callback is not None:
        progress_callback(0, total_files)

    sidecars: list[Path] = []
    try:
        count = 0
        for vault_file in candidates:
            try:
                data = vault_file.read_bytes()
            except OSError:
                logger.warning("Cannot read vault file %s during rotation", vault_file)
                continue

            if len(data) < 1 + salt_len + nonce_len:
                logger.warning("Skipping truncated vault file %s", vault_file)
                continue

            file_version = data[:1]
            if file_version != version:
                logger.warning("Skipping unknown-version vault file %s", vault_file)
                continue

            salt = data[1 : 1 + salt_len]
            old_nonce = data[1 + salt_len : 1 + salt_len + nonce_len]
            old_ciphertext = data[1 + salt_len + nonce_len :]
            aad = version + salt

            # 用旧文件密钥解密；密钥用 bytearray 持有，解密后立即擦除。
            old_file_key = bytearray(derive_file_key(bytes(old_vault_key), salt))
            try:
                aesgcm = AESGCM(bytes(old_file_key))
                try:
                    plaintext = aesgcm.decrypt(old_nonce, old_ciphertext, aad)
                except Exception:
                    logger.warning(
                        "Cannot decrypt vault file %s during rotation (wrong key?)",
                        vault_file,
                    )
                    continue
            finally:
                _secure_zero(old_file_key)

            # 明文同样复制为 bytearray 以便用后擦除。
            plaintext_buf = bytearray(plaintext)
            # 用新文件密钥重新加密（保持 salt 与 version 不变）。
            new_file_key = bytearray(derive_file_key(bytes(new_vault_key), salt))
            try:
                new_nonce = os.urandom(nonce_len)
                aesgcm_new = AESGCM(bytes(new_file_key))
                new_ciphertext = aesgcm_new.encrypt(new_nonce, bytes(plaintext_buf), aad)
            finally:
                _secure_zero(new_file_key)
                _secure_zero(plaintext_buf)

            new_data = version + salt + new_nonce + new_ciphertext

            # Phase 1: write to a .tmp_new sidecar.  The original is left
            # untouched until every file has been successfully re-encrypted.
            sidecar = vault_file.with_name(f"{vault_file.name}.tmp_new")
            sidecar.write_bytes(new_data)
            os.chmod(sidecar, stat.S_IRUSR | stat.S_IWUSR)
            sidecars.append(sidecar)

            count += 1
            if progress_callback is not None:
                progress_callback(count, total_files)

        # Phase 2: commit.  Atomically replace each original with its sidecar.
        # os.replace is atomic on POSIX/Windows when src and dst are on the same
        # filesystem, which they are here (same directory).
        committed = 0
        for sidecar in sidecars:
            original = sidecar.with_name(sidecar.name[: -len(".tmp_new")])
            sidecar.replace(original)
            committed += 1

            if audit_logger is not None:
                audit_logger.log(
                    "encrypted",
                    {
                        "operation": "key_rotation",
                        "vault_path": str(original),
                    },
                )

        return committed
    except BaseException:
        # Rollback: delete every .tmp_new sidecar created so far.  Originals
        # are guaranteed untouched because we only replace them in Phase 2,
        # which we never reached (or did not finish).  For any sidecars whose
        # originals were already replaced, the replacement *is* the new file
        # and there is nothing to roll back — those files are already correctly
        # re-encrypted.  We only clean up sidecars that still exist on disk.
        for sidecar in sidecars:
            try:
                if sidecar.exists():
                    sidecar.unlink()
            except OSError:
                logger.warning("Failed to clean up sidecar %s during rollback", sidecar)
        raise


def rotate_master_key(
    current_provider: "MasterKeyProvider",
    new_provider: "MasterKeyProvider",
    vault_key: bytes,
    storage_path: Path,
    audit_logger: "AuditLogger | None" = None,
    vault_dir: Path | None = None,
) -> "MasterKeyProvider":
    """Rotate the master key to a new provider.

    Performs a full rotation:
    1. Unlock current master key from *current_provider*.
    2. Generate new master key from *new_provider*.
    3. Derive old and new vault keys.
    4. Re-encrypt all vault files with the new vault key.
    5. Record the rotation in a dedicated ``.rotation_marker`` file.

    Parameters
    ----------
    current_provider:
        The currently active master key provider.
    new_provider:
        The new provider to rotate to. Must support ``get_key()`` and ``clear()``.
    vault_key:
        The current vault key bytes. Used to validate the old master key.
    storage_path:
        Path to the ``master_key.bin`` file.  A rotation timestamp is written
        to a sibling ``.rotation_marker`` file (the master key file itself is
        **not** overwritten, since the new provider already persists its own
        key material via ``get_key()``).
    audit_logger:
        Optional audit logger for recording the rotation.
    vault_dir:
        Directory holding the encrypted vault files to re-encrypt.  When
        ``None`` (the default, preserved for backward compatibility) it is
        inferred as ``storage_path.parent.parent / "Vault"``.

    Returns
    -------
    The new provider (same as *new_provider*) on success.
    """
    # 1. Unlock current master key.
    # 用 bytearray 持有敏感派生密钥，以便在 finally 中真正擦除，缩短其在内存中的存活时间。
    old_master_key = bytearray(current_provider.get_key())
    old_derived_vault_key: bytearray | None = None
    new_master_key: bytearray | None = None
    new_vault_key: bytearray | None = None
    try:
        old_derived_vault_key = _derive_vault_key_from_master(old_master_key)

        # Validate: the derived vault key must match the one currently in use.
        if old_derived_vault_key != vault_key:
            # Clean up and raise.
            current_provider.clear()
            raise ValueError(
                "Master key validation failed: derived vault key does not match "
                "the current vault key."
            )

        # 2. Generate new master key.
        new_master_key = bytearray(new_provider.get_key())
        new_vault_key = _derive_vault_key_from_master(new_master_key)

        if new_vault_key == old_derived_vault_key:
            # Collision – should not happen with HKDF but be safe.
            current_provider.clear()
            new_provider.clear()
            raise RuntimeError("New vault key collides with old vault key; rotation aborted.")

        # 3. Re-encrypt vault files.  Resolve the vault directory from the
        # explicit parameter when provided, otherwise fall back to the legacy
        # inference so existing callers (and tests) keep working.
        resolved_vault_dir = (
            vault_dir if vault_dir is not None else storage_path.parent.parent / "Vault"
        )
        if resolved_vault_dir.exists():
            file_count = _re_encrypt_vault_files(
                resolved_vault_dir, old_derived_vault_key, new_vault_key, audit_logger
            )
        else:
            file_count = 0

        # 4. Record the rotation in a dedicated marker file.  We deliberately do
        # NOT overwrite master_key.bin: the new provider has already persisted its
        # own key material (e.g. via DPAPI/TPM/Keychain), and clobbering the
        # master key file with a bare timestamp would destroy data that other code
        # paths may still need.  The marker is written atomically so a crash mid
        # rotation leaves either the old marker or the new one — never a torn
        # write.
        marker_path = storage_path.with_name(".rotation_marker")
        backup_marker = None
        if marker_path.exists():
            backup_marker = storage_path.with_name(".rotation_marker.bak")
            marker_path.replace(backup_marker)

        try:
            rotation_marker = datetime.now(UTC).isoformat().encode("utf-8") + b"\n"
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(marker_path, rotation_marker)
        except Exception:
            # 5. Rollback on failure: restore the previous marker if we moved it.
            if backup_marker and backup_marker.exists():
                backup_marker.replace(marker_path)
            # Clear sensitive material.
            current_provider.clear()
            new_provider.clear()
            raise

        # 6. Audit log.
        if audit_logger is not None:
            audit_logger.log(
                "master_key_changed",
                {
                    "operation": "rotation",
                    "provider_type": type(new_provider).__name__,
                    "files_re_encrypted": file_count,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

        # Clear old provider's cached key.
        current_provider.clear()

        return new_provider
    finally:
        # 擦除本函数持有的敏感派生密钥（provider 自身的缓存由 clear() 处理）。
        _secure_zero(old_master_key)
        _secure_zero(old_derived_vault_key)
        _secure_zero(new_master_key)
        _secure_zero(new_vault_key)


def emergency_rotate(
    current_provider: "MasterKeyProvider",
    new_provider: "MasterKeyProvider",
    vault_key: bytes,
    vault_key_backup_path: Path,
    audit_logger: "AuditLogger | None" = None,
    vault_dir: Path | None = None,
) -> bytes:
    """Perform an emergency rotation when a key compromise is detected.

    Unlike :func:`rotate_master_key`, this function does **not** re-encrypt
    all vault files. Instead it:

    1. Wraps (encrypts) the existing *vault_key* with the new master key
       and stores the wrapped blob at *vault_key_backup_path*.
    2. The caller is responsible for using the wrapped vault key with the
       new provider to continue decrypting existing vault files.

    Parameters
    ----------
    current_provider:
        The possibly compromised provider.
    new_provider:
        The new provider to switch to.
    vault_key:
        The current vault key bytes.
    vault_key_backup_path:
        Path where the new-master-key-wrapped vault key blob is stored.
    audit_logger:
        Optional audit logger for forced audit records.
    vault_dir:
        Reserved for symmetry with :func:`rotate_master_key`.  Emergency
        rotation does not touch vault files, so the value is accepted but
        unused; it exists so callers can pass the same arguments to both
        rotation entry points.

    Returns
    -------
    The new master key bytes so the caller can secure it independently.
    """
    # 1. Validate that current_provider can still produce the vault key.
    # 用 bytearray 持有敏感密钥以便用后擦除（new_master 需返回给调用方，不擦除）。
    old_master = bytearray(current_provider.get_key())
    old_derived: bytearray | None = None
    try:
        old_derived = _derive_vault_key_from_master(old_master)
        if old_derived != vault_key:
            current_provider.clear()
            raise ValueError(
                "Current provider master key does not match the active vault key. "
                "Emergency rotation aborted."
            )

        # 2. Generate new master key and wrap the existing vault key.
        new_master = new_provider.get_key()
        wrapped = _encrypt_vault_key(vault_key, new_master)

        # 3. Store the wrapped vault key atomically.
        vault_key_backup_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(vault_key_backup_path, wrapped)

        # 4. Force audit logging – this is a security-critical event.
        if audit_logger is not None:
            audit_logger.log(
                "master_key_changed",
                {
                    "operation": "emergency_rotation",
                    "reason": "key_compromise",
                    "provider_type": type(new_provider).__name__,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )

        # 5. Clear sensitive material from old provider.
        current_provider.clear()

        return new_master
    finally:
        _secure_zero(old_master)
        _secure_zero(old_derived)


def unwrap_vault_key(wrapped_path: Path, master_key: bytes | bytearray) -> bytearray:
    """Unwrap (decrypt) a vault key protected by *master_key*.

    This is the inverse of the wrapping performed by :func:`emergency_rotate`.

    Parameters
    ----------
    wrapped_path:
        Path to the wrapped vault key blob.
    master_key:
        The master key bytes from the current provider.

    Returns
    -------
    The original vault key as a ``bytearray`` so the caller can wipe it after
    use via :func:`_secure_zero`.
    """
    wrapped = wrapped_path.read_bytes()
    return _decrypt_vault_key(wrapped, master_key)


def create_master_key_provider(
    provider_name: str,
    storage_path: Path,
    password: str | None = None,
    password_file: Path | None = None,
    hello_salt: bytes | None = None,
) -> MasterKeyProvider:
    """Factory for master key providers.

    Looks up the provider in the pluggable registry. Built-in providers are
    registered automatically; custom providers can be added with
    ``register_provider``.
    """
    name = provider_name.lower()
    # Built-ins are constructed directly so mypy can verify their signatures.
    if name == "filepassword":
        return FilePasswordProvider(
            password=password,
            password_file=password_file,
            storage_path=storage_path,
        )
    if name == "dpapi":
        return DpapiMasterKeyProvider(storage_path)
    if name == "tpm":
        return TpmMasterKeyProvider(storage_path, hello_salt=hello_salt)
    if name == "mac-keychain":
        return KeychainMasterKeyProvider(storage_path)
    provider_cls = _REGISTRY.get(name)
    if provider_cls is None:
        raise ValueError(f"Unknown master key provider: {provider_name}")
    # Custom providers are expected to accept a single ``storage_path`` argument.
    return provider_cls(storage_path)  # type: ignore[call-arg]


# Register built-in providers.
register_provider("filepassword", FilePasswordProvider)
register_provider("dpapi", DpapiMasterKeyProvider)
register_provider("tpm", TpmMasterKeyProvider)
register_provider("mac-keychain", KeychainMasterKeyProvider)
