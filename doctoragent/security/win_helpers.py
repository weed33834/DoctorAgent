"""Windows 11 specific security helpers.

These helpers rely on Windows-only APIs. They will raise RuntimeError on non-Windows platforms.
"""

import ctypes
import sys
from typing import ClassVar


def _require_windows() -> None:
    """Raise if not running on Windows."""
    if sys.platform != "win32":
        raise RuntimeError("Windows-specific helper called on a non-Windows platform")


def secure_zero(buf: bytearray) -> None:
    """Overwrite a bytearray with zeros using SecureZeroMemory on Windows."""
    _require_windows()
    if not buf:
        return
    # Create a ctypes array that shares the underlying buffer so the Windows
    # API zeros the original bytearray, not a temporary copy.
    arr = (ctypes.c_char * len(buf)).from_buffer(buf)
    ctypes.windll.kernel32.RtlSecureZeroMemory(arr, len(buf))  # type: ignore[attr-defined]
    for i in range(len(buf)):
        buf[i] = 0


def protect_data(data: bytes) -> bytes:
    """Protect bytes with Windows DPAPI for the current user."""
    _require_windows()
    import ctypes.wintypes as wintypes

    class DATA_BLOB(ctypes.Structure):  # noqa: N801
        _fields_: ClassVar[list[tuple[str, type]]] = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(wintypes.BYTE)),
        ]

    crypt_protect_data = ctypes.windll.crypt32.CryptProtectData  # type: ignore[attr-defined]
    crypt_protect_data.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt_protect_data.restype = wintypes.BOOL

    buffer_in = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(
        cbData=len(data),
        pbData=ctypes.cast(buffer_in, ctypes.POINTER(wintypes.BYTE)),
    )
    blob_out = DATA_BLOB()

    ok = crypt_protect_data(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise RuntimeError("CryptProtectData failed")

    protected = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[attr-defined]
    return bytes(protected)


def unprotect_data(data: bytes) -> bytes:
    """Unprotect bytes previously protected with DPAPI."""
    _require_windows()
    import ctypes.wintypes as wintypes

    class DATA_BLOB(ctypes.Structure):  # noqa: N801
        _fields_: ClassVar[list[tuple[str, type]]] = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(wintypes.BYTE)),
        ]

    crypt_unprotect_data = ctypes.windll.crypt32.CryptUnprotectData  # type: ignore[attr-defined]
    crypt_unprotect_data.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt_unprotect_data.restype = wintypes.BOOL

    buffer_in = ctypes.create_string_buffer(data)
    blob_in = DATA_BLOB(
        cbData=len(data),
        pbData=ctypes.cast(buffer_in, ctypes.POINTER(wintypes.BYTE)),
    )
    blob_out = DATA_BLOB()

    ok = crypt_unprotect_data(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise RuntimeError("CryptUnprotectData failed")

    unprotected = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[attr-defined]
    return bytes(unprotected)
