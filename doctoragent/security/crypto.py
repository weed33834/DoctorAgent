"""AES-256-GCM whole-file and streaming encryption.

Two on-disk formats are supported:

* **v1 (whole-file)** — ``encrypt_file_stream`` / ``decrypt_file_stream`` read
  the entire plaintext / ciphertext into memory before a single AES-256-GCM
  seal/open (atomic temp-file + rename on write).  Best for small files.
* **v2 (streaming)** — ``encrypt_file_streaming`` /
  ``decrypt_file_streaming`` process the file in fixed-size chunks, each with
  its own IV and authentication tag, so the plaintext is never fully resident
  in memory.  Each chunk's AAD binds its position so reordering or dropping a
  chunk is detected.

The dispatcher :func:`encrypt_file` picks v1 or v2 based on a configurable
size threshold (default 10 MB).  :func:`decrypt_file_stream` auto-detects the
format from the version byte so a single decrypt entry point handles both.
"""

import contextlib
import os
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = b"\x01"
# v2 streaming format: chunked AEAD with a final empty sentinel chunk.
VERSION_STREAMING = b"\x02"
SALT_LEN = 32
NONCE_LEN = 12
TAG_LEN = 16

# Size above which :func:`encrypt_file` switches to streaming encryption.
STREAMING_THRESHOLD_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_CHUNK_SIZE = 65536  # 64 KiB
# Wire-format header for each chunk envelope: a 4-byte big-endian uint32
# holding the length of (iv + ciphertext + tag) that follows.
_CHUNK_HEADER_FMT = ">I"
_CHUNK_HEADER_LEN = 4


def _open_no_follow(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open *path* without following symlinks when supported by the OS."""
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, mode)


def _atomic_write_bytes(destination: Path, data: bytes) -> None:
    """Write *data* to a temp file in the same directory, then atomically replace.

    The temp file lives next to *destination* so the final rename stays on a
    single filesystem and is atomic.  If anything fails the temp file is removed
    and *destination* is left untouched.

    The data is flushed and ``fsync``-ed before the rename so a crash after the
    rename never leaves a torn file on disk.  The parent directory is also
    ``fsync``-ed on Linux/macOS so the directory entry durability is ensured.
    """
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path = destination.with_name(f".{destination.name}.{os.urandom(8).hex()}.tmp")
    try:
        fd = _open_no_follow(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as dst:
            dst.write(data)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_path, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Best-effort fsync of *directory* to durably commit a rename/unlink.

    Only meaningful on POSIX filesystems; on Windows this is a no-op.
    """
    if not hasattr(os, "fsync"):
        return
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def encrypt_file_stream(
    source: Path,
    destination: Path,
    key: bytes,
    salt: bytes,
) -> bytes:
    """Encrypt source file to destination using AES-256-GCM.

    File format: [1B version][32B salt][12B nonce][ciphertext][16B tag]

    The ciphertext is buffered and written to a temp file which is atomically
    renamed onto *destination* so a crash never leaves a partially written vault.
    """
    if len(key) != 32:
        raise ValueError("Key must be 32 bytes for AES-256-GCM")
    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_LEN)

    aad = VERSION + salt
    src_fd = _open_no_follow(source, os.O_RDONLY)
    with os.fdopen(src_fd, "rb") as src:
        plaintext = src.read()
    ciphertext = aesgcm.encrypt(nonce, plaintext, aad)

    _atomic_write_bytes(destination, VERSION + salt + nonce + ciphertext)

    # The last TAG_LEN bytes of ciphertext are the GCM authentication tag.
    return nonce


def decrypt_file_stream(
    source: Path,
    destination: Path,
    key: bytes,
) -> None:
    """Decrypt source file to destination using AES-256-GCM.

    Auto-detects the on-disk format from the version byte: v1 (whole-file)
    or v2 (streaming).  The plaintext is fully recovered and authenticated
    before *destination* is touched, so a failed or tampered decryption can
    never truncate an existing destination file.  The write itself is atomic
    (temp file + rename).
    """
    if len(key) != 32:
        raise ValueError("Key must be 32 bytes for AES-256-GCM")
    aesgcm = AESGCM(key)

    src_fd = _open_no_follow(source, os.O_RDONLY)
    with os.fdopen(src_fd, "rb") as src:
        version = src.read(1)
        if version == VERSION:
            salt = src.read(SALT_LEN)
            if len(salt) != SALT_LEN:
                raise ValueError("Truncated vault file: short salt")
            nonce = src.read(NONCE_LEN)
            if len(nonce) != NONCE_LEN:
                raise ValueError("Truncated vault file: short nonce")
            ciphertext = src.read()
            aad = version + salt
            plaintext = aesgcm.decrypt(nonce, ciphertext, aad)
            _atomic_write_bytes(destination, plaintext)
            return
        if version == VERSION_STREAMING:
            plaintext = _decrypt_streaming_body(src, aesgcm)
            _atomic_write_bytes(destination, plaintext)
            return
        raise ValueError("Unsupported vault file version")


# ── Streaming (v2) encryption ────────────────────────────────────────────────


def encrypt_file_streaming(
    source: Path,
    destination: Path,
    key: bytes,
    salt: bytes,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> bytes:
    """Encrypt *source* to *destination* in fixed-size AES-256-GCM chunks.

    The file is read and written one chunk at a time so the plaintext is never
    fully resident in memory.  Wire format::

        [1B version=0x02][32B salt]
        per data chunk:
            [4B big-endian len = 12 + ct_len + 16]
            [12B iv][ciphertext][16B tag]
        final sentinel chunk (empty plaintext):
            [4B len = 28][12B iv][16B tag]

    Each chunk's AAD binds ``version + salt + 8-byte chunk index`` so
    reordering, truncation or chunk removal is detected by the GCM auth tag.

    Returns a 12-byte placeholder nonce (the streaming format has no single
    nonce; the value is kept for ``EncryptResult`` compatibility and is not
    used for decryption).
    """
    if len(key) != 32:
        raise ValueError("Key must be 32 bytes for AES-256-GCM")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if len(salt) != SALT_LEN:
        raise ValueError(f"salt must be {SALT_LEN} bytes")
    aesgcm = AESGCM(key)

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path = destination.with_name(f".{destination.name}.{os.urandom(8).hex()}.tmp")
    # Open the source first so a missing source never leaves a stray temp file.
    src_fd = _open_no_follow(source, os.O_RDONLY)
    try:
        # fdopen takes ownership of the fd on success; on failure we close it
        # manually (mirrors _atomic_write_salt in master_key.py).
        src_fh = os.fdopen(src_fd, "rb")
    except OSError:
        with contextlib.suppress(OSError):
            os.close(src_fd)
        raise
    try:
        fd = _open_no_follow(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            src_fh.close()
        raise
    try:
        dst_fh = os.fdopen(fd, "wb")
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            src_fh.close()
        raise
    try:
        with dst_fh, src_fh:
            dst_fh.write(VERSION_STREAMING + salt)
            index = 0
            while True:
                chunk = src_fh.read(chunk_size)
                iv = os.urandom(NONCE_LEN)
                aad = VERSION_STREAMING + salt + struct.pack(">Q", index)
                ct = aesgcm.encrypt(iv, chunk, aad)  # ciphertext || tag
                envelope = iv + ct
                dst_fh.write(struct.pack(_CHUNK_HEADER_FMT, len(envelope)))
                dst_fh.write(envelope)
                if not chunk:
                    # Empty plaintext marks the sentinel; stream is complete.
                    break
                index += 1
            dst_fh.flush()
            os.fsync(dst_fh.fileno())
        os.replace(tmp_path, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    # No single nonce in streaming mode; return a stable placeholder so the
    # EncryptResult schema (which expects 12 bytes) stays satisfied.
    return b"\x00" * NONCE_LEN


def decrypt_file_streaming(
    source: Path,
    destination: Path,
    key: bytes,
) -> None:
    """Decrypt a v2 streaming vault file chunk-by-chunk.

    Reads and authenticates one chunk at a time, writing the recovered
    plaintext to a temp file that is atomically renamed onto *destination*
    only after the whole stream verifies — so a tampered or truncated stream
    can never leave a partial destination file.
    """
    if len(key) != 32:
        raise ValueError("Key must be 32 bytes for AES-256-GCM")
    aesgcm = AESGCM(key)

    src_fd = _open_no_follow(source, os.O_RDONLY)
    with os.fdopen(src_fd, "rb") as src:
        version = src.read(1)
        if version != VERSION_STREAMING:
            raise ValueError("Not a streaming vault file (version mismatch)")
        salt = src.read(SALT_LEN)
        if len(salt) != SALT_LEN:
            raise ValueError("Truncated streaming vault file: short salt")
        plaintext = _decrypt_streaming_body(src, aesgcm, version=version, salt=salt)
    _atomic_write_bytes(destination, plaintext)


def _decrypt_streaming_body(
    src,
    aesgcm: AESGCM,
    version: bytes = VERSION_STREAMING,
    salt: bytes | None = None,
) -> bytes:
    """Decrypt the chunk body of a v2 stream from an open file object *src*.

    If *salt* is None the salt is read from *src* first (used by the v1
    dispatcher which has already consumed version + salt).
    """
    if salt is None:
        salt = src.read(SALT_LEN)
        if len(salt) != SALT_LEN:
            raise ValueError("Truncated streaming vault file: short salt")
    chunks: list[bytes] = []
    index = 0
    while True:
        header = src.read(_CHUNK_HEADER_LEN)
        if len(header) == 0:
            # EOF reached without a sentinel chunk → the stream was truncated.
            raise ValueError("Truncated streaming vault file: missing sentinel")
        if len(header) < _CHUNK_HEADER_LEN:
            raise ValueError("Truncated streaming vault file: short chunk header")
        (envelope_len,) = struct.unpack(_CHUNK_HEADER_FMT, header)
        if envelope_len < NONCE_LEN + TAG_LEN:
            raise ValueError("Invalid streaming chunk envelope: too short")
        envelope = src.read(envelope_len)
        if len(envelope) < envelope_len:
            raise ValueError("Truncated streaming vault file: short chunk body")
        iv = envelope[:NONCE_LEN]
        ct_with_tag = envelope[NONCE_LEN:]
        aad = version + salt + struct.pack(">Q", index)
        plaintext = aesgcm.decrypt(iv, ct_with_tag, aad)
        if not plaintext:
            # Sentinel: end of stream.
            break
        chunks.append(plaintext)
        index += 1
    return b"".join(chunks)


# ── Dispatcher ───────────────────────────────────────────────────────────────


def encrypt_file(
    source: Path,
    destination: Path,
    key: bytes,
    salt: bytes,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    streaming_threshold: int = STREAMING_THRESHOLD_BYTES,
) -> bytes:
    """Encrypt *source*, picking whole-file or streaming AEAD by size.

    Files larger than *streaming_threshold* (default 10 MB) are encrypted
    with :func:`encrypt_file_streaming` so the plaintext is never fully loaded
    into memory; smaller files use the atomic whole-file :func:`encrypt_file_stream`.

    Returns the 12-byte nonce (whole-file) or placeholder (streaming) for
    ``EncryptResult`` compatibility.
    """
    try:
        size = source.stat().st_size
    except OSError:
        size = 0
    if size > streaming_threshold:
        nonce = encrypt_file_streaming(source, destination, key, salt, chunk_size)
        _incr_encryption_metric("encrypt_streaming")
        return nonce
    nonce = encrypt_file_stream(source, destination, key, salt)
    _incr_encryption_metric("encrypt_whole")
    return nonce


def _incr_encryption_metric(op: str) -> None:
    """Increment the ``doctoragent_encryption_ops_total`` counter.

    Safe no-op when prometheus_client is absent (the metric is an in-process
    stub) or when observability failed to import for any reason.
    """
    try:
        from doctoragent.observability.metrics import doctoragent_encryption_ops_total

        doctoragent_encryption_ops_total.labels(op=op).inc()
    except Exception:  # noqa: BLE001 - metrics must never break crypto
        pass
