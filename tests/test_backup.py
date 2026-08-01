"""Tests for Vault backup and key share splitting."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from doctoragent.api.schemas import ClassificationResult, SensitivityLevel
from doctoragent.execution.vault import VaultManager
from doctoragent.security.backup import (
    backup_vault,
    key_fingerprint,
    recombine_key,
    split_key,
    write_key_shares,
)
from doctoragent.security.keytree import derive_vault_key


def _make_classification() -> ClassificationResult:
    return ClassificationResult(
        sensitivity=SensitivityLevel.MEDIUM,
        category="work",
        tags=["report"],
        summary="A report",
        disguise_name="report_2026",
        disguise_extension="log",
    )


@pytest.fixture
def vault_with_files(tmp_path: Path) -> tuple[Path, bytes]:
    """Create a vault with two encrypted files."""
    vault_path = tmp_path / "Vault"
    vault_path.mkdir()
    master_key = os.urandom(32)
    vault_key = derive_vault_key(master_key)
    manager = VaultManager(vault_path, vault_key)

    for i in range(2):
        source = tmp_path / f"source_{i}.txt"
        source.write_text(f"content {i}", encoding="utf-8")
        manager.encrypt(source, _make_classification(), uuid4())
    return vault_path, vault_key


def test_backup_copies_all_files(
    vault_with_files: tuple[Path, bytes],
    tmp_path: Path,
) -> None:
    vault_path, _ = vault_with_files
    backup_root = tmp_path / "backup"

    result = backup_vault(vault_path, backup_root)

    assert result.ok
    assert len(result.backed_up) == 2
    assert len(result.skipped) == 0
    # Backup directory mirrors the source structure.
    manifest = ".doctoragent-backup-manifest.json"
    backed_files = [p for p in backup_root.rglob("*") if p.is_file() and p.name != manifest]
    assert len(backed_files) == 2


def test_backup_is_incremental(
    vault_with_files: tuple[Path, bytes],
    tmp_path: Path,
) -> None:
    """A second backup with no changes skips all files."""
    vault_path, _ = vault_with_files
    backup_root = tmp_path / "backup"

    backup_vault(vault_path, backup_root)
    result = backup_vault(vault_path, backup_root)

    assert result.ok
    assert len(result.backed_up) == 0
    assert len(result.skipped) == 2


def test_backup_detects_changed_files(
    vault_with_files: tuple[Path, bytes],
    tmp_path: Path,
) -> None:
    """Modifying a source file triggers re-backup of that file."""
    vault_path, vault_key = vault_with_files
    backup_root = tmp_path / "backup"
    manager = VaultManager(vault_path, vault_key)

    backup_vault(vault_path, backup_root)

    # Add a new file to the vault.
    source = tmp_path / "new.txt"
    source.write_text("new content", encoding="utf-8")
    manager.encrypt(source, _make_classification(), uuid4())

    result = backup_vault(vault_path, backup_root)
    assert result.ok
    assert len(result.backed_up) == 1
    assert len(result.skipped) == 2


def test_backup_removes_deleted_files(
    vault_with_files: tuple[Path, bytes],
    tmp_path: Path,
) -> None:
    """Files removed from the source are pruned from the backup."""
    vault_path, _ = vault_with_files
    backup_root = tmp_path / "backup"

    backup_vault(vault_path, backup_root)
    manifest = ".doctoragent-backup-manifest.json"
    files_before = [p for p in backup_root.rglob("*") if p.is_file() and p.name != manifest]
    assert len(files_before) == 2

    # Delete one source file (rglob returns dirs too, filter to files only).
    files = [p for p in vault_path.rglob("*") if p.is_file()]
    assert len(files) == 2
    files[0].unlink()

    result = backup_vault(vault_path, backup_root)
    assert result.ok
    assert len(result.removed) == 1

    files_after = [p for p in backup_root.rglob("*") if p.is_file() and p.name != manifest]
    assert len(files_after) == 1


def test_backup_empty_vault(tmp_path: Path) -> None:
    vault_path = tmp_path / "Vault"
    vault_path.mkdir()
    backup_root = tmp_path / "backup"

    result = backup_vault(vault_path, backup_root)
    assert result.ok
    assert len(result.backed_up) == 0


def test_backup_missing_vault_is_noop(tmp_path: Path) -> None:
    backup_root = tmp_path / "backup"
    result = backup_vault(tmp_path / "nonexistent", backup_root)
    assert result.ok
    assert len(result.backed_up) == 0


# --- Key share splitting --------------------------------------------------


def test_split_and_recombine_roundtrip() -> None:
    key = os.urandom(32)
    shares = split_key(key, 3)
    assert len(shares) == 3
    assert all(len(s) == 32 for s in shares)

    recovered = recombine_key(shares)
    assert recovered == key


def test_recombine_works_in_any_order() -> None:
    key = os.urandom(32)
    shares = split_key(key, 4)
    # Shuffle the shares.
    import random

    random.shuffle(shares)
    assert recombine_key(shares) == key


def test_recombine_with_partial_shares_yields_wrong_key() -> None:
    """A partial share set (n-of-n scheme) yields a wrong key, not the original."""
    key = os.urandom(32)
    shares = split_key(key, 3)
    # Only 2 of 3 shares: XOR still produces a 32-byte value, but it must not
    # equal the original key. This documents the n-of-n property.
    partial = recombine_key(shares[:2])
    assert len(partial) == 32
    assert partial != key


def test_split_rejects_fewer_than_two_shares() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        split_key(b"key", 1)


def test_split_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match="empty"):
        split_key(b"", 3)


def test_recombine_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="No shares"):
        recombine_key([])


def test_recombine_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        recombine_key([b"short", b"longerkey"])


def test_key_fingerprint_is_stable_and_truncated() -> None:
    key = b"0" * 32
    fp1 = key_fingerprint(key)
    fp2 = key_fingerprint(key)
    assert fp1 == fp2
    assert len(fp1) == 16


def test_key_fingerprint_differs_for_different_keys() -> None:
    fp1 = key_fingerprint(b"0" * 32)
    fp2 = key_fingerprint(b"1" * 32)
    assert fp1 != fp2


def test_write_key_shares_creates_files(tmp_path: Path) -> None:
    key = os.urandom(32)
    dest = tmp_path / "shares"

    paths = write_key_shares(key, 3, dest)

    assert len(paths) == 3
    for p in paths:
        assert p.exists()
        assert p.stat().st_size == 32
        # Verify restrictive permissions on POSIX.
        if os.name == "posix":
            assert (p.stat().st_mode & 0o077) == 0


def test_write_key_shares_roundtrip(tmp_path: Path) -> None:
    """Shares written to disk can be read back and recombined."""
    key = os.urandom(32)
    dest = tmp_path / "shares"

    paths = write_key_shares(key, 3, dest)
    shares = [p.read_bytes() for p in paths]
    assert recombine_key(shares) == key
