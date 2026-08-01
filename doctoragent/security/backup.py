"""Vault backup and disaster recovery.

Two concerns:

1. Vault content backup. Copies encrypted Vault files to a backup location,
   transferring only files whose mtime changed since the last backup (an
   incremental manifest is stored alongside the backup). Source files are
   already encrypted on disk, so the backup copy needs no additional
   encryption.

2. Key material recovery. Splits the master key into N shares using XOR
   sharing (all N shares required to reconstruct). This is a simple
   n-of-n scheme suitable for a single user distributing shares across
   trusted locations (safe, USB stick, password manager). It is NOT a
   threshold scheme like Shamir; for k-of-n recovery use a dedicated
   library. The trade-off is zero dependencies and an auditable 40-line
   implementation.

   .. note::
      **建议 (Suggestion):** If threshold (k-of-n) secret sharing is ever
      needed, replace ``split_key``/``recombine_key`` with an
      implementation of **Shamir's Secret Sharing** (e.g. the
      ``shamir`` or ``ssss`` PyPI package). The current XOR n-of-n
      scheme is intentionally dependency-free for portability; a Shamir
      library was not adopted due to potential cross-platform
      compatibility concerns, but should be re-evaluated when a
      k-of-n recovery policy is required.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_MANIFEST_NAME = ".doctoragent-backup-manifest.json"
_MANIFEST_VERSION = 1


@dataclass
class BackupResult:
    """Outcome of a single backup run."""

    backed_up: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _load_manifest(manifest_path: Path) -> dict[str, float]:
    """Load the backup manifest, mapping relative path -> source mtime."""
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load backup manifest %s: %s", manifest_path, exc)
        return {}
    if data.get("version") != _MANIFEST_VERSION:
        return {}
    return {k: float(v) for k, v in data.get("entries", {}).items()}


def _save_manifest(manifest_path: Path, entries: dict[str, float]) -> None:
    """Persist the manifest atomically."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": _MANIFEST_VERSION, "entries": entries}
    tmp = manifest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, manifest_path)


def _iter_vault_files(vault_path: Path) -> list[Path]:
    if not vault_path.exists():
        return []
    return sorted(p for p in vault_path.rglob("*") if p.is_file())


def backup_vault(
    vault_path: Path,
    backup_root: Path,
) -> BackupResult:
    """Incrementally back up *vault_path* into *backup_root*.

    Only files whose mtime differs from the last recorded manifest entry are
    copied. Files present in the backup but no longer in the source are removed
    (the backup mirrors the source). The manifest is updated atomically at the
    end.
    """
    result = BackupResult()
    manifest_path = backup_root / _MANIFEST_NAME
    backup_root.mkdir(parents=True, exist_ok=True)

    prev_entries = _load_manifest(manifest_path)
    current_entries: dict[str, float] = {}
    source_files = _iter_vault_files(vault_path)
    source_rel = {p.relative_to(vault_path): p for p in source_files}

    # Copy changed files.
    for rel, src in source_rel.items():
        try:
            mtime = src.stat().st_mtime
        except OSError as exc:
            result.error = f"stat failed for {src}: {exc}"
            return result
        current_entries[str(rel)] = mtime
        dst = backup_root / rel
        if prev_entries.get(str(rel)) == mtime and dst.exists():
            result.skipped.append(src)
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Copy via temp + rename to keep the backup atomic per file.
            tmp = dst.with_name(f".{dst.name}.{os.urandom(4).hex()}.tmp")
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            result.backed_up.append(src)
        except OSError as exc:
            result.error = f"copy failed for {src}: {exc}"
            return result

    # Remove files that disappeared from the source.
    for rel_str in list(prev_entries):
        if rel_str not in current_entries:
            stale = backup_root / rel_str
            if stale.exists():
                try:
                    stale.unlink()
                    result.removed.append(stale)
                except OSError as exc:
                    logger.warning("Failed to remove stale backup %s: %s", stale, exc)

    try:
        _save_manifest(manifest_path, current_entries)
    except OSError as exc:
        result.error = f"manifest write failed: {exc}"
        return result

    logger.info(
        "Vault backup complete: %d copied, %d skipped, %d removed",
        len(result.backed_up),
        len(result.skipped),
        len(result.removed),
    )
    return result


# --- Key share splitting (n-of-n XOR) --------------------------------------


def split_key(key: bytes, shares: int) -> list[bytes]:
    """Split *key* into *shares* parts using XOR n-of-n sharing.

    All *shares* parts are required to reconstruct the key. The first
    ``shares-1`` parts are random; the last part XORs them with the key.

    Use cases: distributing a master key across a safe, a USB stick, and a
    password manager so that compromising one location does not reveal the key.

    Raises ValueError if *shares* < 2.
    """
    if shares < 2:
        raise ValueError("Need at least 2 shares for splitting")
    if not key:
        raise ValueError("Cannot split an empty key")
    random_parts = [os.urandom(len(key)) for _ in range(shares - 1)]
    xor_acc = bytes(len(key))
    for part in random_parts:
        xor_acc = bytes(a ^ b for a, b in zip(xor_acc, part, strict=True))
    last = bytes(a ^ b for a, b in zip(xor_acc, key, strict=True))
    return random_parts + [last]


def recombine_key(shares: list[bytes]) -> bytes:
    """Reconstruct the key from all *shares* produced by :func:`split_key`.

    The shares can be supplied in any order. Raises ValueError if shares is
    empty or the lengths are inconsistent.
    """
    if not shares:
        raise ValueError("No shares provided")
    length = len(shares[0])
    if any(len(s) != length for s in shares):
        raise ValueError("All shares must have the same length")
    xor_acc = bytes(length)
    for share in shares:
        xor_acc = bytes(a ^ b for a, b in zip(xor_acc, share, strict=True))
    return xor_acc


def key_fingerprint(key: bytes) -> str:
    """Return a short hex fingerprint of *key* for verification.

    The fingerprint is SHA-256 truncated to 16 hex chars. It lets a user verify
    they recombined the correct shares without exposing the key itself.
    """
    return hashlib.sha256(key).hexdigest()[:16]


def write_key_shares(
    key: bytes,
    shares: int,
    dest_dir: Path,
    *,
    prefix: str = "doctoragent-key-share",
) -> list[Path]:
    """Split *key* and write each share to *dest_dir* as a separate file.

    Files are named ``{prefix}-{n}-of-{shares}`` and written with 0o600
    permissions. Returns the list of written paths.
    """
    parts = split_key(key, shares)
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for idx, part in enumerate(parts, start=1):
        path = dest_dir / f"{prefix}-{idx}-of-{shares}"
        # Write atomically with restrictive permissions.
        tmp = path.with_name(f".{path.name}.{os.urandom(4).hex()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(part)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        written.append(path)
    logger.info("Wrote %d key shares to %s", shares, dest_dir)
    return written
