"""Tests for the sync engine (engine.py)."""

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from doctoragent.sync.auth import DeviceAuth
from doctoragent.sync.discovery import DeviceDiscovery
from doctoragent.sync.engine import SyncEngine
from doctoragent.sync.protocol import FileIndex, SecureSyncProtocol

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def shared_secret() -> bytes:
    return os.urandom(32)


@pytest.fixture
def vault_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "config"


@pytest.fixture
def protocol(shared_secret: bytes) -> SecureSyncProtocol:
    return SecureSyncProtocol("device-test", shared_secret)


@pytest.fixture
def discovery() -> DeviceDiscovery:
    return DeviceDiscovery("test-device", device_id="device-test")


@pytest.fixture
def auth(config_dir: Path) -> DeviceAuth:
    return DeviceAuth(config_dir)


@pytest.fixture
def engine(
    vault_dir: Path,
    protocol: SecureSyncProtocol,
    discovery: DeviceDiscovery,
    auth: DeviceAuth,
) -> SyncEngine:
    return SyncEngine(vault_dir, protocol, discovery, auth)


# ── Construction ─────────────────────────────────────────────────────────────


class TestConstruction:
    def test_creates_vault_dir(
        self,
        tmp_path: Path,
        protocol: SecureSyncProtocol,
        discovery: DeviceDiscovery,
        auth: DeviceAuth,
    ) -> None:
        vp = tmp_path / "new_vault"
        _ = SyncEngine(vp, protocol, discovery, auth)
        assert vp.exists()
        assert vp.is_dir()

    def test_loads_sync_state_empty(self, engine: SyncEngine) -> None:
        assert engine._sync_state.last_sync_time == {}
        assert engine._sync_state.known_files == {}

    def test_custom_vault_key(
        self,
        tmp_path: Path,
        protocol: SecureSyncProtocol,
        discovery: DeviceDiscovery,
        auth: DeviceAuth,
    ) -> None:
        key = os.urandom(32)
        engine = SyncEngine(tmp_path / "v", protocol, discovery, auth, vault_key=key)
        assert engine.vault_key == key

    def test_default_vault_key_is_shared_secret(
        self,
        vault_dir: Path,
        shared_secret: bytes,
        discovery: DeviceDiscovery,
        auth: DeviceAuth,
    ) -> None:
        proto = SecureSyncProtocol("d", shared_secret)
        engine = SyncEngine(vault_dir, proto, discovery, auth)
        assert engine.vault_key == shared_secret


# ── build_index ──────────────────────────────────────────────────────────────


class TestBuildIndex:
    @pytest.mark.asyncio
    async def test_empty_vault(self, engine: SyncEngine) -> None:
        index = await engine.build_index()
        assert index.files == {}
        assert index.device_id == "device-test"

    @pytest.mark.asyncio
    async def test_single_file(self, engine: SyncEngine) -> None:
        (engine.vault_path / "hello.txt").write_text("hello world")
        index = await engine.build_index()
        assert "hello.txt" in index.files
        entry = index.files["hello.txt"]
        assert "hash" in entry
        assert entry["size"] == 11
        assert "mtime" in entry

    @pytest.mark.asyncio
    async def test_multiple_files(self, engine: SyncEngine) -> None:
        (engine.vault_path / "a.txt").write_text("aaa")
        (engine.vault_path / "b.txt").write_text("bbb")
        index = await engine.build_index()
        assert set(index.files) == {"a.txt", "b.txt"}

    @pytest.mark.asyncio
    async def test_nested_directories(self, engine: SyncEngine) -> None:
        sub = engine.vault_path / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("nested")
        index = await engine.build_index()
        assert "sub/nested.txt" in index.files

    @pytest.mark.asyncio
    async def test_hash_deterministic(self, engine: SyncEngine) -> None:
        (engine.vault_path / "data.bin").write_bytes(b"deterministic")
        idx1 = await engine.build_index()
        idx2 = await engine.build_index()
        assert idx1.files["data.bin"]["hash"] == idx2.files["data.bin"]["hash"]

    @pytest.mark.asyncio
    async def test_skips_sync_state_file(self, engine: SyncEngine) -> None:
        (engine.vault_path / "real.txt").write_text("real")
        (engine.vault_path / ".sync_state.json").write_text("secret")
        index = await engine.build_index()
        assert ".sync_state.json" not in index.files
        assert "real.txt" in index.files

    @pytest.mark.asyncio
    async def test_handles_unreadable_file(self, engine: SyncEngine) -> None:
        """Files that cannot be read are skipped."""
        (engine.vault_path / "good.txt").write_text("ok")
        bad = engine.vault_path / "bad.txt"
        bad.write_text("will-vanish")
        bad.chmod(0o000)
        try:
            index = await engine.build_index()
            assert "good.txt" in index.files
        finally:
            bad.chmod(0o644)


# ── detect_changes ───────────────────────────────────────────────────────────


class TestDetectChanges:
    @pytest.mark.asyncio
    async def test_no_changes(self, engine: SyncEngine) -> None:
        old_idx = FileIndex(
            files={"a.txt": {"hash": "x", "size": 3, "mtime": 1.0, "device_id": "d"}},
            snapshot_time=1.0,
            device_id="d",
        )
        new_idx = FileIndex(
            files={"a.txt": {"hash": "x", "size": 3, "mtime": 2.0, "device_id": "d"}},
            snapshot_time=2.0,
            device_id="d",
        )
        added, modified, deleted = await engine.detect_changes(old_idx, new_idx)
        assert added == []
        assert modified == []
        assert deleted == []

    @pytest.mark.asyncio
    async def test_added_file(self, engine: SyncEngine) -> None:
        old_idx = FileIndex(files={}, snapshot_time=1.0, device_id="d")
        new_idx = FileIndex(
            files={"new.txt": {"hash": "y", "size": 1, "mtime": 1.0, "device_id": "d"}},
            snapshot_time=2.0,
            device_id="d",
        )
        added, modified, deleted = await engine.detect_changes(old_idx, new_idx)
        assert len(added) == 1
        assert added[0]["hash"] == "y"

    @pytest.mark.asyncio
    async def test_deleted_file(self, engine: SyncEngine) -> None:
        old_idx = FileIndex(
            files={"old.txt": {"hash": "x", "size": 1, "mtime": 1.0, "device_id": "d"}},
            snapshot_time=1.0,
            device_id="d",
        )
        new_idx = FileIndex(files={}, snapshot_time=2.0, device_id="d")
        added, modified, deleted = await engine.detect_changes(old_idx, new_idx)
        assert deleted == ["old.txt"]

    @pytest.mark.asyncio
    async def test_modified_file(self, engine: SyncEngine) -> None:
        old_idx = FileIndex(
            files={"f.txt": {"hash": "old_hash", "size": 3, "mtime": 1.0, "device_id": "d"}},
            snapshot_time=1.0,
            device_id="d",
        )
        new_idx = FileIndex(
            files={"f.txt": {"hash": "new_hash", "size": 5, "mtime": 2.0, "device_id": "d"}},
            snapshot_time=2.0,
            device_id="d",
        )
        added, modified, deleted = await engine.detect_changes(old_idx, new_idx)
        assert len(modified) == 1
        assert modified[0]["hash"] == "new_hash"

    @pytest.mark.asyncio
    async def test_mixed_changes(self, engine: SyncEngine) -> None:
        old_idx = FileIndex(
            files={
                "same.txt": {"hash": "x", "size": 1, "mtime": 1.0, "device_id": "d"},
                "changed.txt": {"hash": "old", "size": 1, "mtime": 1.0, "device_id": "d"},
                "gone.txt": {"hash": "x", "size": 1, "mtime": 1.0, "device_id": "d"},
            },
            snapshot_time=1.0,
            device_id="d",
        )
        new_idx = FileIndex(
            files={
                "same.txt": {"hash": "x", "size": 1, "mtime": 2.0, "device_id": "d"},
                "changed.txt": {"hash": "new", "size": 2, "mtime": 2.0, "device_id": "d"},
                "added.txt": {"hash": "y", "size": 1, "mtime": 2.0, "device_id": "d"},
            },
            snapshot_time=2.0,
            device_id="d",
        )
        added, modified, deleted = await engine.detect_changes(old_idx, new_idx)
        assert len(added) == 1
        assert added[0]["hash"] == "y"
        assert len(modified) == 1
        assert modified[0]["hash"] == "new"
        assert deleted == ["gone.txt"]


# ── FileIndex ────────────────────────────────────────────────────────────────


class TestFileIndex:
    def test_defaults(self) -> None:
        fi = FileIndex()
        assert fi.files == {}
        assert fi.snapshot_time == 0.0
        assert fi.device_id == ""

    def test_to_dict(self) -> None:
        fi = FileIndex(
            files={"a": {"hash": "x"}},
            snapshot_time=1.0,
            device_id="d1",
        )
        d = fi.to_dict()
        assert d["files"] == {"a": {"hash": "x"}}
        assert d["snapshot_time"] == 1.0
        assert d["device_id"] == "d1"

    def test_from_dict(self) -> None:
        d = {"files": {"b": {"hash": "y"}}, "snapshot_time": 2.0, "device_id": "d2"}
        fi = FileIndex.from_dict(d)
        assert fi.files == {"b": {"hash": "y"}}
        assert fi.snapshot_time == 2.0
        assert fi.device_id == "d2"

    def test_roundtrip(self) -> None:
        fi = FileIndex(
            files={"c": {"hash": "z", "size": 10}},
            snapshot_time=3.0,
            device_id="dev",
        )
        assert FileIndex.from_dict(fi.to_dict()) == fi
