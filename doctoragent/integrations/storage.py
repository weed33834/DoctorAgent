"""Pluggable remote storage backends for encrypted offsite backup.

Phase 7.3 extends the local-only :func:`doctoragent.security.backup.backup_vault`
with two remote transports:

* **S3 / MinIO** — object storage via the SigV4 API. Works with AWS S3,
  MinIO, DigitalOcean Spaces, and any S3-compatible endpoint.
* **WebDAV** — HTTP-based remote filesystem, common for Nextcloud, ownCloud,
  and Synology NAS boxes.

Both transports carry **already-encrypted** vault bytes: the vault files on
disk are AES-256-GCM ciphertext, so the backend never sees plaintext and no
additional envelope encryption is required. This matches the threat model of
the local backup — the remote store is treated as untrusted storage.

Backend selection is driven by ``integrations.storage_backup_backend``:
``"local"`` (default, no-op remote), ``"s3"``, or ``"webdav"``. Credentials
live in environment variables (see :class:`~doctoragent.config.IntegrationsConfig`)
and are never persisted to the settings file.

Each backend operation emits a ``storage_backend_operation`` audit event so
offsite writes are visible in the tamper-evident log.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from doctoragent.config import IntegrationsConfig
    from doctoragent.security.audit_log import AuditLogger

logger = logging.getLogger(__name__)


class StorageBackendError(RuntimeError):
    """Raised when a storage backend operation fails or config is invalid."""


@dataclass
class StorageObject:
    """A single remote object listing entry."""

    key: str
    size: int
    last_modified: str  # ISO-8601 from the remote, or "" if unknown


@dataclass
class BackupTransferResult:
    """Outcome of a backup-to-backend run."""

    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class StorageBackend(ABC):
    """Abstract storage backend.

    Implementations carry already-encrypted bytes; they never handle
    plaintext vault content. All methods raise :class:`StorageBackendError`
    on failure so callers can wrap a whole backup in a single try/except.
    """

    backend_name: str = "abstract"

    def __init__(self, *, prefix: str = "") -> None:
        self._prefix = prefix

    @abstractmethod
    def test_connection(self) -> bool:
        """Return True if the backend is reachable and credentials work."""

    @abstractmethod
    def upload(self, local_path: Path, remote_key: str) -> None:
        """Upload *local_path* to *remote_key* (relative to the configured prefix)."""

    @abstractmethod
    def download(self, remote_key: str, local_path: Path) -> None:
        """Download *remote_key* to *local_path*."""

    @abstractmethod
    def list(self, prefix: str = "") -> list[StorageObject]:
        """List objects under *prefix* (relative to the configured prefix)."""

    @abstractmethod
    def delete(self, remote_key: str) -> None:
        """Delete *remote_key* from the backend."""

    def _full_key(self, remote_key: str) -> str:
        """Join *remote_key* with this backend's configured prefix."""
        return f"{self._prefix}{remote_key}" if self._prefix else remote_key


# ── Local backend (pass-through to filesystem) ──────────────────────────────


class LocalBackend(StorageBackend):
    """Filesystem backend — mirrors the existing local backup behaviour.

    Used as the default and as a test stand-in. ``root`` is the destination
    directory; uploads are atomic (temp + rename).
    """

    backend_name = "local"

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def test_connection(self) -> bool:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            return self._root.is_dir()
        except OSError:
            return False

    def upload(self, local_path: Path, remote_key: str) -> None:
        dest = self._root / remote_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Atomic upload: copy to a temp file in the same dir, then rename.
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".upload.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(local_path.read_bytes())
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def download(self, remote_key: str, local_path: Path) -> None:
        src = self._root / remote_key
        if not src.is_file():
            raise StorageBackendError(f"local backend: object not found: {remote_key}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(src.read_bytes())

    def list(self, prefix: str = "") -> list[StorageObject]:
        base = self._root / prefix
        if not base.exists():
            return []
        objects: list[StorageObject] = []
        if base.is_file():
            st = base.stat()
            objects.append(
                StorageObject(
                    key=str(Path(prefix)).replace("\\", "/"),
                    size=st.st_size,
                    last_modified="",
                )
            )
            return objects
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self._root)
                st = p.stat()
                objects.append(
                    StorageObject(
                        key=str(rel).replace("\\", "/"), size=st.st_size, last_modified=""
                    )
                )
        return objects

    def delete(self, remote_key: str) -> None:
        target = self._root / remote_key
        if target.exists():
            target.unlink()


# ── S3 / MinIO backend (optional boto3) ─────────────────────────────────────


class S3Backend(StorageBackend):
    """S3-compatible object storage backend (AWS S3, MinIO, Spaces, …).

    Uses ``boto3`` if installed; raises :class:`StorageBackendError` on
    construction if ``boto3`` is missing so the failure surfaces early
    rather than at first upload.
    """

    backend_name = "s3"

    def __init__(self, config: IntegrationsConfig) -> None:
        super().__init__(prefix=config.storage_key_prefix)
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — exercised only without boto3
            raise StorageBackendError("S3 backend requires boto3: pip install boto3") from exc
        if not config.s3_bucket:
            raise StorageBackendError("S3 backend: s3_bucket is required")
        self._bucket = config.s3_bucket
        self._client = boto3.client(  # type: ignore[attr-defined]
            "s3",
            endpoint_url=config.s3_endpoint or None,
            region_name=config.s3_region,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            config=boto3.session.Config(  # type: ignore[attr-defined]
                s3={"addressing_style": "path" if config.s3_use_path_style else "auto"},
            ),
        )

    def test_connection(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self._bucket)  # type: ignore[no-any-return]
            return True
        except Exception:
            logger.warning("S3 backend: head_bucket failed", exc_info=True)
            return False

    def upload(self, local_path: Path, remote_key: str) -> None:
        key = self._full_key(remote_key)
        try:
            self._client.upload_file(str(local_path), self._bucket, key)
        except Exception as exc:  # noqa: BLE001
            raise StorageBackendError(f"S3 upload failed for {remote_key}: {exc}") from exc

    def download(self, remote_key: str, local_path: Path) -> None:
        key = self._full_key(remote_key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(self._bucket, key, str(local_path))
        except Exception as exc:  # noqa: BLE001
            raise StorageBackendError(f"S3 download failed for {remote_key}: {exc}") from exc

    def list(self, prefix: str = "") -> list[StorageObject]:
        full_prefix = self._full_key(prefix)
        try:
            resp = self._client.list_objects_v2(Bucket=self._bucket, Prefix=full_prefix)
        except Exception as exc:  # noqa: BLE001
            raise StorageBackendError(f"S3 list failed: {exc}") from exc
        objects: list[StorageObject] = []
        for obj in resp.get("Contents", []) or []:
            key = str(obj.get("Key", ""))
            # Strip the configured prefix so callers see relative keys.
            if self._prefix and key.startswith(self._prefix):
                key = key[len(self._prefix) :]
            last_mod = obj.get("LastModified")
            last_mod_str = last_mod.isoformat() if last_mod is not None else ""
            objects.append(
                StorageObject(
                    key=key,
                    size=int(obj.get("Size", 0)),
                    last_modified=last_mod_str,
                )
            )
        return objects

    def delete(self, remote_key: str) -> None:
        key = self._full_key(remote_key)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            raise StorageBackendError(f"S3 delete failed for {remote_key}: {exc}") from exc


# ── WebDAV backend (httpx-based) ─────────────────────────────────────────────


class WebDAVBackend(StorageBackend):
    """WebDAV remote filesystem backend.

    Implemented over httpx (already a hard dependency) using the small
    subset of WebDAV verbs needed for backup: ``PUT``, ``GET``, ``PROPFIND``,
    ``DELETE``. Works with Nextcloud, ownCloud, and standard mod_dav servers.
    """

    backend_name = "webdav"

    def __init__(self, config: IntegrationsConfig) -> None:
        super().__init__(prefix=config.storage_key_prefix)
        if not config.webdav_url:
            raise StorageBackendError("WebDAV backend: webdav_url is required")
        self._base_url = config.webdav_url.rstrip("/")
        self._auth = (
            (config.webdav_username or "", config.webdav_password or "")
            if (config.webdav_username or config.webdav_password)
            else None
        )
        # Lazy import so unit tests that inject a fake client don't require httpx.
        self._client_factory: Any = None

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import httpx

        return httpx.Client(base_url=self._base_url, auth=self._auth, timeout=30.0)

    def test_connection(self) -> bool:
        try:
            client = self._client()
            # PROPFIND on the base URL confirms auth + WebDAV support.
            resp = client.request(
                "PROPFIND",
                "/",
                headers={"Depth": "0"},
            )
            return resp.status_code in (200, 207)
        except Exception:
            logger.warning("WebDAV backend: connection test failed", exc_info=True)
            return False

    def upload(self, local_path: Path, remote_key: str) -> None:
        key = self._full_key(remote_key)
        try:
            client = self._client()
            # Ensure parent collections exist (best-effort MKCOL).
            self._ensure_parent_collections(client, key)
            with open(local_path, "rb") as fh:
                resp = client.put(f"/{key}", content=fh.read())
            if resp.status_code not in (200, 201, 204):
                raise StorageBackendError(
                    f"WebDAV upload failed for {remote_key}: HTTP {resp.status_code}"
                )
        except StorageBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageBackendError(f"WebDAV upload failed for {remote_key}: {exc}") from exc

    def download(self, remote_key: str, local_path: Path) -> None:
        key = self._full_key(remote_key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            client = self._client()
            resp = client.get(f"/{key}")
            if resp.status_code != 200:
                raise StorageBackendError(
                    f"WebDAV download failed for {remote_key}: HTTP {resp.status_code}"
                )
            local_path.write_bytes(resp.content)
        except StorageBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageBackendError(f"WebDAV download failed for {remote_key}: {exc}") from exc

    def list(self, prefix: str = "") -> list[StorageObject]:
        full_prefix = self._full_key(prefix)
        try:
            client = self._client()
            resp = client.request(
                "PROPFIND",
                f"/{full_prefix}",
                headers={"Depth": "1"},
            )
        except Exception as exc:  # noqa: BLE001
            raise StorageBackendError(f"WebDAV list failed: {exc}") from exc
        if resp.status_code not in (200, 207):
            raise StorageBackendError(f"WebDAV list failed: HTTP {resp.status_code}")
        return _parse_propfind_xml(resp.text, full_prefix, self._prefix)

    def delete(self, remote_key: str) -> None:
        key = self._full_key(remote_key)
        try:
            client = self._client()
            resp = client.delete(f"/{key}")
            if resp.status_code not in (200, 204, 404):
                raise StorageBackendError(
                    f"WebDAV delete failed for {remote_key}: HTTP {resp.status_code}"
                )
        except StorageBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StorageBackendError(f"WebDAV delete failed for {remote_key}: {exc}") from exc

    def _ensure_parent_collections(self, client: Any, key: str) -> None:
        """Best-effort MKCOL for each parent path segment.

        WebDAV servers reject PUT to a path whose parent collection does not
        exist. We MKCOL each ancestor; failures are ignored because the
        collection may already exist (405/409) or the server may auto-create.
        """
        parts = key.split("/")[:-1]  # drop the filename
        path = ""
        for part in parts:
            path = f"{path}/{part}" if path else part
            try:
                client.request("MKCOL", f"/{path}")
            except Exception:
                # Ignore — collection likely already exists.
                pass


def _parse_propfind_xml(
    xml_text: str,
    request_prefix: str,
    configured_prefix: str,
) -> list[StorageObject]:
    """Parse a PROPFIND multistatus body into StorageObject entries.

    Uses xml.etree (stdlib) with namespace-agnostic tag matching so it
    tolerates the varied namespace decls different servers emit. Extracted
    to module scope so the ``list`` return annotation is not shadowed by
    :meth:`WebDAVBackend.list`.
    """
    import xml.etree.ElementTree as ElementTree

    objects: list[StorageObject] = []
    try:
        root = ElementTree.fromstring(xml_text)  # nosec B314
    except ElementTree.ParseError:
        return objects
    # WebDAV multistatus responses live under any namespace ending in
    # "multistatus"; each <response> has an <href> and <propstat>.
    for response in root.iter():
        tag = response.tag.split("}")[-1]
        if tag != "response":
            continue
        href = None
        size = 0
        last_mod = ""
        for child in response.iter():
            ctag = child.tag.split("}")[-1]
            if ctag == "href" and href is None:
                href = child.text or ""
            elif ctag == "getcontentlength" and child.text:
                try:
                    size = int(child.text)
                except ValueError:
                    pass
            elif ctag == "getlastmodified" and child.text:
                last_mod = child.text
        if href is None:
            continue
        # Normalise the href back to a relative key by stripping the
        # base URL path and the configured prefix.
        from urllib.parse import unquote

        key = unquote(href)
        # Anchor prefix matching to the first path segment: strip a leading
        # slash first so a configured prefix cannot match inside a later path
        # segment (e.g. prefix "ab" must not match "/cab/..."), then strip the
        # configured prefix when present at the start.
        if key.startswith("/"):
            key = key.lstrip("/")
        if configured_prefix and key.startswith(configured_prefix):
            key = key[len(configured_prefix) :]
        # Skip the collection itself (the prefix dir).
        if not key or key == request_prefix or key.endswith("/"):
            continue
        objects.append(StorageObject(key=key, size=size, last_modified=last_mod))
    return objects


# ── Factory ─────────────────────────────────────────────────────────────────


def create_storage_backend(
    config: IntegrationsConfig,
    *,
    local_root: Path | None = None,
) -> StorageBackend:
    """Build the configured storage backend.

    For ``storage_backup_backend == "local"`` *local_root* must be supplied
    (the caller picks the backup directory). For ``"s3"``/``"webdav"`` the
    credentials come from the config block.
    """
    name = config.storage_backup_backend.lower()
    if name == "local":
        if local_root is None:
            raise StorageBackendError("local backend requires local_root")
        return LocalBackend(local_root)
    if name == "s3":
        return S3Backend(config)
    if name == "webdav":
        return WebDAVBackend(config)
    raise StorageBackendError(f"unknown storage backend: {name!r}")


# ── Backup orchestration ────────────────────────────────────────────────────


@contextlib.contextmanager
def _tmp_json_path() -> Iterator[Path]:
    """Yield a freshly-created temp ``.json`` path, unlinked on exit."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    path = Path(tmp)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def backup_vault_to_backend(
    vault_path: Path,
    backend: StorageBackend,
    *,
    audit_logger: AuditLogger | None = None,
    manifest_key: str = ".doctoragent-backup-manifest.json",
) -> BackupTransferResult:
    """Incrementally back up *vault_path* into *backend*.

    Mirrors the local :func:`backup_vault` logic: only files whose mtime
    changed since the last manifest entry are uploaded, and files that
    disappeared from the source are deleted from the backend. The manifest
    itself is stored as a remote object so the backup is resumable from any
    machine with access to the same backend.

    Each upload/delete emits a ``storage_backend_operation`` audit event;
    a terminal failure sets ``result.error`` and stops the run.
    """
    result = BackupTransferResult()

    source_files = sorted(p for p in vault_path.rglob("*") if p.is_file() and not p.is_symlink())
    source_rel = {p.relative_to(vault_path): p for p in source_files}
    current_entries: dict[str, float] = {}

    # Load the previous manifest from the backend (if any).
    prev_entries: dict[str, float] = {}
    with _tmp_json_path() as tmp_manifest:
        try:
            backend.download(manifest_key, tmp_manifest)
            data = json.loads(tmp_manifest.read_text(encoding="utf-8"))
            prev_entries = {k: float(v) for k, v in data.get("entries", {}).items()}
        except StorageBackendError:
            # No manifest yet — first backup.
            pass
        except (json.JSONDecodeError, ValueError):
            logger.warning("Remote backup manifest corrupt; treating as empty")

    # Upload changed files.
    for rel, src in source_rel.items():
        rel_str = str(rel).replace("\\", "/")
        try:
            mtime = src.stat().st_mtime
        except OSError as exc:
            result.error = f"stat failed for {src}: {exc}"
            break
        current_entries[rel_str] = mtime
        if prev_entries.get(rel_str) == mtime:
            # Verify the remote object still exists before skipping.
            existing = {o.key for o in backend.list(rel_str)}
            if rel_str in existing:
                result.skipped.append(rel_str)
                continue
        try:
            backend.upload(src, rel_str)
            result.uploaded.append(rel_str)
            _emit_storage_audit(
                audit_logger,
                backend.backend_name,
                "upload",
                rel_str,
                success=True,
            )
        except StorageBackendError as exc:
            result.error = f"upload failed for {rel_str}: {exc}"
            _emit_storage_audit(
                audit_logger,
                backend.backend_name,
                "upload",
                rel_str,
                success=False,
                error=str(exc),
            )
            break

    # Remove files that disappeared from the source.
    if result.error is None:
        for rel_str in list(prev_entries):
            if rel_str not in current_entries:
                try:
                    backend.delete(rel_str)
                    result.removed.append(rel_str)
                    _emit_storage_audit(
                        audit_logger,
                        backend.backend_name,
                        "delete",
                        rel_str,
                        success=True,
                    )
                except StorageBackendError as exc:
                    logger.warning("Failed to delete stale remote object %s: %s", rel_str, exc)

    # Persist the updated manifest.
    if result.error is None:
        manifest_payload = {"version": 1, "entries": current_entries}
        with _tmp_json_path() as tmp_manifest:
            tmp_manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
            try:
                backend.upload(tmp_manifest, manifest_key)
            except StorageBackendError as exc:
                result.error = f"manifest upload failed: {exc}"

    logger.info(
        "Remote backup to %s complete: %d uploaded, %d skipped, %d removed",
        backend.backend_name,
        len(result.uploaded),
        len(result.skipped),
        len(result.removed),
    )
    return result


def _emit_storage_audit(
    audit_logger: AuditLogger | None,
    backend_name: str,
    operation: str,
    key: str,
    *,
    success: bool,
    error: str = "",
) -> None:
    """Best-effort audit emission for a storage backend operation."""
    if audit_logger is None:
        return
    details: dict[str, Any] = {
        "backend": backend_name,
        "operation": operation,
        "key": key,
        "success": success,
    }
    if not success:
        details["error"] = error
        details["severity"] = "HIGH"
    try:
        audit_logger.log("storage_backend_operation", details)
    except Exception:  # pragma: no cover — audit must not break backup
        logger.exception("Failed to emit storage_backend_operation audit event")
