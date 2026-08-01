"""Tests for conflict detection and resolution (conflict.py)."""

from doctoragent.sync.conflict import (
    Conflict,
    ConflictDetector,
    ConflictResolver,
    CRDTMerge,
    KeepBoth,
    LastWriteWins,
    ManualResolve,
)
from doctoragent.sync.protocol import FileIndex

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_index(device_id: str, files: dict[str, dict[str, object]]) -> FileIndex:
    return FileIndex(files=files, snapshot_time=100.0, device_id=device_id)


# ── Conflict dataclass ──────��────────────────────────────────────────────────


class TestConflict:
    def test_conflict_defaults(self) -> None:
        c = Conflict(file_path="test.txt")
        assert c.file_path == "test.txt"
        assert c.local_version == {}
        assert c.remote_version == {}
        assert c.conflict_type == ""

    def test_conflict_concurrent_edit(self) -> None:
        c = Conflict(
            file_path="doc.txt",
            local_version={"hash": "aaa", "mtime": 1.0},
            remote_version={"hash": "bbb", "mtime": 2.0},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        assert c.conflict_type == "concurrent_edit"
        assert c.local_version["hash"] == "aaa"


# ── ConflictDetector ─────────────────────────────────────────────────────────


class TestConflictDetector:
    def test_no_conflicts_identical(self) -> None:
        detector = ConflictDetector()
        local = _make_index("d1", {"a.txt": {"hash": "x", "mtime": 1.0}})
        remote = _make_index("d2", {"a.txt": {"hash": "x", "mtime": 2.0}})
        assert detector.detect(local, remote) == []

    def test_concurrent_edit_different_hash(self) -> None:
        detector = ConflictDetector()
        local = _make_index("d1", {"a.txt": {"hash": "x", "mtime": 1.0}})
        remote = _make_index("d2", {"a.txt": {"hash": "y", "mtime": 2.0}})
        conflicts = detector.detect(local, remote)
        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == Conflict.TYPE_CONCURRENT_EDIT

    def test_no_conflict_local_only_new(self) -> None:
        detector = ConflictDetector()
        local = _make_index("d1", {"new.txt": {"hash": "x", "mtime": 1.0}})
        remote = _make_index("d2", {})
        assert detector.detect(local, remote) == []

    def test_delete_vs_edit_with_known_files(self) -> None:
        detector = ConflictDetector(known_files={"a.txt": "old_hash"})
        local = _make_index("d1", {"a.txt": {"hash": "new_local", "mtime": 2.0}})
        remote = _make_index("d2", {})
        conflicts = detector.detect(local, remote)
        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == Conflict.TYPE_DELETE_VS_EDIT

    def test_delete_vs_edit_remote_deleted(self) -> None:
        detector = ConflictDetector(known_files={"a.txt": "old_hash"})
        local = _make_index("d1", {})
        remote = _make_index("d2", {"a.txt": {"hash": "new_remote", "mtime": 2.0}})
        conflicts = detector.detect(local, remote)
        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == Conflict.TYPE_DELETE_VS_EDIT

    def test_multiple_conflicts(self) -> None:
        detector = ConflictDetector()
        local = _make_index(
            "d1",
            {
                "a.txt": {"hash": "x", "mtime": 1.0},
                "b.txt": {"hash": "b1", "mtime": 1.0},
            },
        )
        remote = _make_index(
            "d2",
            {
                "a.txt": {"hash": "y", "mtime": 2.0},
                "b.txt": {"hash": "b2", "mtime": 2.0},
            },
        )
        conflicts = detector.detect(local, remote)
        assert len(conflicts) == 2


# ── LastWriteWins ────────────────────────────────────────────────────────────


class TestLastWriteWins:
    def test_keep_remote_newer(self) -> None:
        resolver = LastWriteWins()
        conflict = Conflict(
            file_path="doc.txt",
            local_version={"hash": "a", "mtime": 1.0},
            remote_version={"hash": "b", "mtime": 2.0},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["action"] == "keep_remote"

    def test_keep_local_when_newer(self) -> None:
        resolver = LastWriteWins()
        conflict = Conflict(
            file_path="doc.txt",
            local_version={"hash": "a", "mtime": 3.0},
            remote_version={"hash": "b", "mtime": 2.0},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["action"] == "keep_local"

    def test_keep_local_when_same_mtime(self) -> None:
        resolver = LastWriteWins()
        conflict = Conflict(
            file_path="doc.txt",
            local_version={"hash": "a", "mtime": 1.0},
            remote_version={"hash": "b", "mtime": 1.0},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        # local >= remote → keep_local
        assert result["action"] == "keep_local"

    def test_delete_vs_edit_keep_local(self) -> None:
        resolver = LastWriteWins()
        conflict = Conflict(
            file_path="doc.txt",
            local_version={"hash": "a", "mtime": 2.0},
            remote_version={},
            conflict_type=Conflict.TYPE_DELETE_VS_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["action"] == "keep_local"

    def test_delete_vs_edit_keep_remote(self) -> None:
        resolver = LastWriteWins()
        conflict = Conflict(
            file_path="doc.txt",
            local_version={},
            remote_version={"hash": "b", "mtime": 2.0},
            conflict_type=Conflict.TYPE_DELETE_VS_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["action"] == "keep_remote"


# ── KeepBoth ─────────────────────────────────────────────────────────────────


class TestKeepBoth:
    def test_returns_keep_both(self) -> None:
        resolver = KeepBoth()
        conflict = Conflict(
            file_path="doc.txt",
            local_version={"hash": "a", "mtime": 1.0},
            remote_version={"hash": "b", "mtime": 2.0},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["action"] == "keep_both"
        assert "conflict_path" in result
        assert ".conflict." in result["conflict_path"]

    def test_conflict_path_extension(self) -> None:
        resolver = KeepBoth()
        conflict = Conflict(
            file_path="data.json",
            local_version={"hash": "a", "mtime": 1.0},
            remote_version={"hash": "b", "mtime": 2.0},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["conflict_path"].startswith("data.conflict.")
        assert result["conflict_path"].endswith(".json")

    def test_conflict_path_no_extension(self) -> None:
        resolver = KeepBoth()
        conflict = Conflict(
            file_path="README",
            local_version={"hash": "a", "mtime": 1.0},
            remote_version={"hash": "b", "mtime": 2.0},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["conflict_path"].startswith("README.conflict.")
        assert ".conflict." in result["conflict_path"]


# ── ManualResolve ────────────────────────────────────────────────────────────


class TestManualResolve:
    def test_returns_manual(self) -> None:
        resolver = ManualResolve()
        conflict = Conflict(
            file_path="doc.txt",
            local_version={"hash": "a", "mtime": 1.0},
            remote_version={"hash": "b", "mtime": 2.0},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["action"] == "manual"

    def test_includes_hashes(self) -> None:
        resolver = ManualResolve()
        conflict = Conflict(
            file_path="doc.txt",
            local_version={"hash": "abc123", "mtime": 1.0},
            remote_version={"hash": "def456", "mtime": 2.0},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["local_hash"] == "abc123"
        assert result["remote_hash"] == "def456"


# ── CRDTMerge ────────────────────────────────────────────────────────────────


class TestCRDTMerge:
    def test_merge_union_of_keys(self) -> None:
        resolver = CRDTMerge()
        conflict = Conflict(
            file_path="state.json",
            local_version={"hash": "a", "mtime": 1.0, "data": {"k1": "v1"}},
            remote_version={"hash": "b", "mtime": 2.0, "data": {"k2": "v2"}},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["action"] == "merge"
        assert result["merged_data"] == {"k1": "v1", "k2": "v2"}

    def test_merge_conflict_prefers_remote_newer(self) -> None:
        resolver = CRDTMerge()
        conflict = Conflict(
            file_path="state.json",
            local_version={"hash": "a", "mtime": 1.0, "data": {"k": "local"}},
            remote_version={"hash": "b", "mtime": 2.0, "data": {"k": "remote"}},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["merged_data"] == {"k": "remote"}

    def test_merge_conflict_prefers_local_when_newer(self) -> None:
        resolver = CRDTMerge()
        conflict = Conflict(
            file_path="state.json",
            local_version={"hash": "a", "mtime": 3.0, "data": {"k": "local"}},
            remote_version={"hash": "b", "mtime": 2.0, "data": {"k": "remote"}},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["merged_data"] == {"k": "local"}

    def test_merge_handles_string_data(self) -> None:
        resolver = CRDTMerge()
        import json

        conflict = Conflict(
            file_path="state.json",
            local_version={
                "hash": "a",
                "mtime": 1.0,
                "data": json.dumps({"a": 1}),
            },
            remote_version={
                "hash": "b",
                "mtime": 2.0,
                "data": json.dumps({"b": 2}),
            },
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["action"] == "merge"
        assert result["merged_data"] == {"a": 1, "b": 2}

    def test_merge_empty_versions(self) -> None:
        resolver = CRDTMerge()
        conflict = Conflict(
            file_path="state.json",
            local_version={},
            remote_version={},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert result["action"] == "merge"
        assert result["merged_data"] == {}

    def test_merge_includes_hash(self) -> None:
        resolver = CRDTMerge()
        conflict = Conflict(
            file_path="state.json",
            local_version={"hash": "a", "mtime": 1.0, "data": {"x": 1}},
            remote_version={"hash": "b", "mtime": 2.0, "data": {"y": 2}},
            conflict_type=Conflict.TYPE_CONCURRENT_EDIT,
        )
        result = resolver.resolve(conflict, _make_index("d1", {}), _make_index("d2", {}))
        assert "merged_hash" in result
        assert len(result["merged_hash"]) == 64  # SHA-256 hex


# ── Abstract base ────────────────────────────────────────────────────────────


def test_conflict_resolver_is_abstract() -> None:
    """ConflictResolver should not be directly instantiable."""
    import pytest  # noqa: F811

    with pytest.raises(TypeError):
        ConflictResolver()  # type: ignore[abstract]
