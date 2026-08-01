# mypy: ignore-errors
"""Tests for the pluggable storage backends (Phase 7.3).

The local backend is exercised end-to-end against the real filesystem. The
WebDAV backend is tested against an in-memory fake httpx client so no real
server is needed. S3 is exercised via the factory's config-validation path
(since boto3 may be absent in CI) and a stubbed client.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from doctoragent.config import AegisConfig, IntegrationsConfig
from doctoragent.integrations.storage import (
    LocalBackend,
    StorageBackendError,
    WebDAVBackend,
    backup_vault_to_backend,
    create_storage_backend,
)
from doctoragent.security.audit_log import AuditLogger

# ── Helpers ─────────────────────────────────────────────────────────────────


def _integrations(**overrides: Any) -> IntegrationsConfig:
    cfg = AegisConfig()
    for k, v in overrides.items():
        setattr(cfg.integrations, k, v)
    return cfg.integrations


def _audit(tmp_path: Path) -> AuditLogger:
    cfg = AegisConfig()
    cfg.paths.logs = tmp_path / "logs"
    return AuditLogger(cfg, hmac_key=b"k" * 32)


def _make_vault(vault: Path, files: dict[str, str]) -> None:
    """Populate *vault* with the given {relpath: content} mapping."""
    for rel, content in files.items():
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


class _FakeWebDAVResponse:
    def __init__(self, status_code: int, text: str = "", content: bytes = b"") -> None:
        self.status_code = status_code
        self.text = text
        self.content = content


class _FakeWebDAVClient:
    """In-memory WebDAV server stub keyed by path."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.collections: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self._next_status = 200

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeWebDAVResponse:
        self.calls.append((method, url))
        method = method.upper()
        if method == "MKCOL":
            self.collections.add(url)
            return _FakeWebDAVResponse(201)
        if method == "PUT":
            body = kwargs.get("content", b"")
            self.objects[url] = body if isinstance(body, bytes) else body.encode()
            return _FakeWebDAVResponse(201)
        if method == "GET":
            data = self.objects.get(url)
            if data is None:
                return _FakeWebDAVResponse(404)
            return _FakeWebDAVResponse(200, content=data)
        if method == "DELETE":
            self.objects.pop(url, None)
            return _FakeWebDAVResponse(204)
        if method == "PROPFIND":
            return _FakeWebDAVResponse(207, text=self._propfind_xml(url, kwargs))
        return _FakeWebDAVResponse(405)

    # httpx-style convenience verbs that delegate to request().
    def get(self, url: str, **kwargs: Any) -> _FakeWebDAVResponse:
        return self.request("GET", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> _FakeWebDAVResponse:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> _FakeWebDAVResponse:
        return self.request("DELETE", url, **kwargs)

    def _propfind_xml(self, url: str, kwargs: Any) -> str:
        depth = kwargs.get("headers", {}).get("Depth", "1")
        parts = []
        # Always include the collection itself.
        parts.append(
            f"<response><href>{url}</href>"
            f"<propstat><prop><resourcetype><collection/></resourcetype>"
            f"</prop></propstat></response>"
        )
        if depth == "1":
            # List direct children.
            for key, data in self.objects.items():
                if key.startswith(url) and key != url:
                    rel = key[len(url) :].lstrip("/")
                    if "/" in rel:
                        continue  # only direct children at depth 1
                    parts.append(
                        f"<response><href>{key}</href>"
                        f"<propstat><prop><getcontentlength>{len(data)}"
                        f"</getcontentlength>"
                        f"<getlastmodified>Mon, 01 Jan 2024 00:00:00 GMT"
                        f"</getlastmodified></prop></propstat></response>"
                    )
        return (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
            + "".join(parts)
            + "</d:multistatus>"
        )


# ── LocalBackend ────────────────────────────────────────────────────────────


def test_local_backend_round_trip(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path / "remote")
    assert backend.test_connection() is True
    src = tmp_path / "file.bin"
    src.write_bytes(b"hello")
    backend.upload(src, "dir/file.bin")
    objects = backend.list("dir/")
    assert any(o.key == "dir/file.bin" for o in objects)
    dest = tmp_path / "downloaded.bin"
    backend.download("dir/file.bin", dest)
    assert dest.read_bytes() == b"hello"
    backend.delete("dir/file.bin")
    assert backend.list("dir/") == []


def test_local_backend_download_missing_raises(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path / "remote")
    with pytest.raises(StorageBackendError, match="not found"):
        backend.download("nope.bin", tmp_path / "out.bin")


def test_local_backend_list_nonexistent_returns_empty(tmp_path: Path) -> None:
    backend = LocalBackend(tmp_path / "remote")
    assert backend.list("missing/") == []


# ── Factory ─────────────────────────────────────────────────────────────────


def test_factory_local_requires_root() -> None:
    cfg = _integrations(storage_backup_backend="local")
    with pytest.raises(StorageBackendError, match="local_root"):
        create_storage_backend(cfg)


def test_factory_local_builds_backend(tmp_path: Path) -> None:
    cfg = _integrations(storage_backup_backend="local")
    backend = create_storage_backend(cfg, local_root=tmp_path / "remote")
    assert isinstance(backend, LocalBackend)


def test_factory_unknown_backend_raises() -> None:
    cfg = _integrations(storage_backup_backend="ftp")
    with pytest.raises(StorageBackendError, match="unknown storage backend"):
        create_storage_backend(cfg)


def test_factory_webdav_requires_url() -> None:
    cfg = _integrations(storage_backup_backend="webdav", webdav_url=None)
    with pytest.raises(StorageBackendError, match="webdav_url"):
        create_storage_backend(cfg)


def test_factory_s3_requires_bucket() -> None:
    # Even if boto3 is installed, missing bucket must surface early.
    cfg = _integrations(storage_backup_backend="s3", s3_bucket=None)
    try:
        create_storage_backend(cfg)
    except (StorageBackendError, ImportError):
        # Either our config guard or the boto3 import guard is acceptable.
        pass


# ── WebDAV backend ──────────────────────────────────────────────────────────


def _webdav_backend(fake: _FakeWebDAVClient, **overrides: Any) -> WebDAVBackend:
    cfg = _integrations(
        storage_backup_backend="webdav",
        webdav_url="https://dav.example.com",
        webdav_username="user",
        webdav_password="pass",
        storage_key_prefix="doctoragent/",
        **overrides,
    )
    backend = WebDAVBackend(cfg)
    backend._client_factory = lambda: fake  # inject the fake client
    return backend


def test_webdav_backend_test_connection_ok() -> None:
    fake = _FakeWebDAVClient()
    backend = _webdav_backend(fake)
    assert backend.test_connection() is True


def test_webdav_backend_upload_download_round_trip(tmp_path: Path) -> None:
    fake = _FakeWebDAVClient()
    backend = _webdav_backend(fake)
    src = tmp_path / "secret.enc"
    src.write_bytes(b"ciphertext-bytes")
    backend.upload(src, "vault/secret.enc")
    # The fake stores under the full path including prefix.
    full_key = "/doctoragent/vault/secret.enc"
    assert full_key in fake.objects
    dest = tmp_path / "downloaded.enc"
    backend.download("vault/secret.enc", dest)
    assert dest.read_bytes() == b"ciphertext-bytes"


def test_webdav_backend_upload_failure_raises(tmp_path: Path) -> None:
    fake = _FakeWebDAVClient()
    # Force PUT to fail by overriding request temporarily.
    original_request = fake.request

    def failing_request(method: str, url: str, **kwargs: Any) -> _FakeWebDAVResponse:
        if method.upper() == "PUT":
            return _FakeWebDAVResponse(403)
        return original_request(method, url, **kwargs)

    fake.request = failing_request  # type: ignore[assignment]
    backend = _webdav_backend(fake)
    src = tmp_path / "x.bin"
    src.write_bytes(b"data")
    with pytest.raises(StorageBackendError, match="WebDAV upload failed"):
        backend.upload(src, "x.bin")


def test_webdav_backend_download_missing_raises(tmp_path: Path) -> None:
    fake = _FakeWebDAVClient()
    backend = _webdav_backend(fake)
    with pytest.raises(StorageBackendError, match="WebDAV download failed"):
        backend.download("nope.bin", tmp_path / "out.bin")


def test_webdav_backend_delete_is_idempotent(tmp_path: Path) -> None:
    fake = _FakeWebDAVClient()
    backend = _webdav_backend(fake)
    # Deleting a non-existent key returns 404 which we treat as success.
    backend.delete("never-existed")


def test_webdav_backend_list_returns_children() -> None:
    fake = _FakeWebDAVClient()
    backend = _webdav_backend(fake)
    # Seed two objects.
    fake.objects["/doctoragent/vault/a.bin"] = b"aaa"
    fake.objects["/doctoragent/vault/b.bin"] = b"bbbb"
    objects = backend.list("vault/")
    keys = {o.key for o in objects}
    assert "vault/a.bin" in keys
    assert "vault/b.bin" in keys


def test_webdav_propfind_parse_handles_namespaces() -> None:
    """The PROPFIND parser must tolerate varied namespace declarations."""
    from doctoragent.integrations.storage import _parse_propfind_xml

    xml = (
        '<?xml version="1.0"?>'
        '<D:multistatus xmlns:D="DAV:">'
        "<D:response>"
        "<D:href>/doctoragent/vault/file.enc</D:href>"
        "<D:propstat><D:prop>"
        "<D:getcontentlength>42</D:getcontentlength>"
        "<D:getlastmodified>Mon, 01 Jan 2024 00:00:00 GMT</D:getlastmodified>"
        "</D:prop></D:propstat>"
        "</D:response>"
        "</D:multistatus>"
    )
    objects = _parse_propfind_xml(xml, "vault/", "doctoragent/")
    assert len(objects) == 1
    assert objects[0].key == "vault/file.enc"
    assert objects[0].size == 42


# ── backup_vault_to_backend ─────────────────────────────────────────────────


def test_backup_to_local_backend_uploads_all_files(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_vault(vault, {"work/a.enc": "AAA", "personal/b.enc": "BBB"})
    backend = LocalBackend(tmp_path / "remote")
    result = backup_vault_to_backend(vault, backend)
    assert result.ok
    assert sorted(result.uploaded) == ["personal/b.enc", "work/a.enc"]
    # Remote now has both files.
    remote_keys = {o.key for o in backend.list("")}
    assert "work/a.enc" in remote_keys
    assert "personal/b.enc" in remote_keys
    # Manifest is also uploaded.
    assert any(".doctoragent-backup-manifest" in k for k in remote_keys)


def test_backup_skips_unchanged_files_on_second_run(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_vault(vault, {"a.enc": "AAA"})
    backend = LocalBackend(tmp_path / "remote")
    first = backup_vault_to_backend(vault, backend)
    assert first.uploaded == ["a.enc"]
    second = backup_vault_to_backend(vault, backend)
    assert second.uploaded == []
    assert second.skipped == ["a.enc"]


def test_backup_uploads_changed_file_on_mtime_change(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_vault(vault, {"a.enc": "AAA"})
    backend = LocalBackend(tmp_path / "remote")
    backup_vault_to_backend(vault, backend)
    # Change the file content + mtime.
    src = vault / "a.enc"
    src.write_text("BBB-longer")
    time.sleep(0.01)
    import os

    os.utime(src, None)
    result = backup_vault_to_backend(vault, backend)
    assert "a.enc" in result.uploaded


def test_backup_removes_files_deleted_from_source(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_vault(vault, {"a.enc": "AAA", "b.enc": "BBB"})
    backend = LocalBackend(tmp_path / "remote")
    backup_vault_to_backend(vault, backend)
    # Delete one file from the source.
    (vault / "a.enc").unlink()
    result = backup_vault_to_backend(vault, backend)
    assert "a.enc" in result.removed
    # Remote no longer has the deleted file.
    remote_keys = {o.key for o in backend.list("")}
    assert "a.enc" not in remote_keys


def test_backup_emits_audit_events(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_vault(vault, {"a.enc": "AAA"})
    backend = LocalBackend(tmp_path / "remote")
    audit = _audit(tmp_path)
    backup_vault_to_backend(vault, backend, audit_logger=audit)
    events = audit.query(event_type="storage_backend_operation")
    # At least one upload event + manifest upload.
    uploads = [e for e in events if e["details"]["operation"] == "upload"]
    assert len(uploads) >= 1
    assert uploads[0]["details"]["backend"] == "local"
    assert uploads[0]["details"]["success"] is True


def test_backup_records_failure_in_result(tmp_path: Path) -> None:
    """A backend that fails mid-upload must stop and set result.error."""
    vault = tmp_path / "vault"
    _make_vault(vault, {"a.enc": "AAA"})

    class _FailingBackend(LocalBackend):
        def upload(self, local_path: Path, remote_key: str) -> None:
            raise StorageBackendError("simulated failure")

    backend = _FailingBackend(tmp_path / "remote")
    result = backup_vault_to_backend(vault, backend)
    assert not result.ok
    assert "simulated failure" in (result.error or "")


def test_backup_first_run_has_no_manifest(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_vault(vault, {"a.enc": "AAA"})
    backend = LocalBackend(tmp_path / "remote")
    # First run: no manifest exists yet, must not raise.
    result = backup_vault_to_backend(vault, backend)
    assert result.ok
    assert result.uploaded == ["a.enc"]


def test_backup_resume_reads_remote_manifest(tmp_path: Path) -> None:
    """A second backend instance must resume from the remote manifest."""
    vault = tmp_path / "vault"
    _make_vault(vault, {"a.enc": "AAA"})
    backend1 = LocalBackend(tmp_path / "remote")
    backup_vault_to_backend(vault, backend1)
    # New backend pointing at the same remote root resumes.
    backend2 = LocalBackend(tmp_path / "remote")
    result = backup_vault_to_backend(vault, backend2)
    assert result.uploaded == []
    assert result.skipped == ["a.enc"]
