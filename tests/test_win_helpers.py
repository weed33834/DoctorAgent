# mypy: ignore-errors
"""Tests for Windows-specific security helpers.

These helpers are designed to run on Windows; on Linux we verify graceful
degradation and exercise the Windows code paths with mocked APIs.
"""

import ctypes
import sys
import types
from unittest.mock import MagicMock

import pytest

from doctoragent.security import win_helpers


def test_require_windows_raises_on_linux() -> None:
    """Non-Windows platforms get a clear RuntimeError immediately."""
    if sys.platform == "win32":
        pytest.skip("Only relevant on non-Windows platforms")
    with pytest.raises(RuntimeError, match="non-Windows platform"):
        win_helpers._require_windows()


def test_secure_zero_raises_on_linux() -> None:
    """secure_zero refuses to run on Linux."""
    if sys.platform == "win32":
        pytest.skip("Only relevant on non-Windows platforms")
    with pytest.raises(RuntimeError, match="non-Windows platform"):
        win_helpers.secure_zero(bytearray(b"secret"))


def test_protect_data_raises_on_linux() -> None:
    """protect_data refuses to run on Linux."""
    if sys.platform == "win32":
        pytest.skip("Only relevant on non-Windows platforms")
    with pytest.raises(RuntimeError, match="non-Windows platform"):
        win_helpers.protect_data(b"secret")


def test_unprotect_data_raises_on_linux() -> None:
    """unprotect_data refuses to run on Linux."""
    if sys.platform == "win32":
        pytest.skip("Only relevant on non-Windows platforms")
    with pytest.raises(RuntimeError, match="non-Windows platform"):
        win_helpers.unprotect_data(b"protected")


def test_secure_zero_empty_buffer() -> None:
    """secure_zero handles an empty buffer without touching Windows APIs."""
    if sys.platform == "win32":
        pytest.skip("Only relevant on non-Windows platforms")
    # Empty buffer should still raise because of _require_windows.
    with pytest.raises(RuntimeError, match="non-Windows platform"):
        win_helpers.secure_zero(bytearray())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API test stub")
def test_secure_zero_zeros_buffer() -> None:
    """On Windows, secure_zero overwrites the whole bytearray."""
    import ctypes
    try:
        ctypes.windll.kernel32.RtlSecureZeroMemory  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pytest.skip("RtlSecureZeroMemory not available on this Windows build")
    buf = bytearray(b"hello world")
    win_helpers.secure_zero(buf)
    assert buf == bytearray(len(buf))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API test stub")
def test_protect_unprotect_data_round_trip() -> None:
    """On Windows, protect/unprotect are inverses for the current user."""
    original = b"doctoragent-test-data"
    protected = win_helpers.protect_data(original)
    assert protected != original
    unprotected = win_helpers.unprotect_data(protected)
    assert unprotected == original


def test_protect_data_failure_path() -> None:
    """protect_data propagates a RuntimeError when the Windows call fails."""
    if sys.platform == "win32":
        pytest.skip("Only relevant on non-Windows platforms")
    # On Linux the function raises before reaching the API, so this is mostly a
    # compile-time/shape check. On Windows it would exercise the failure branch.
    with pytest.raises(RuntimeError):
        win_helpers.protect_data(b"secret")


def test_unprotect_data_failure_path() -> None:
    """unprotect_data propagates a RuntimeError when the Windows call fails."""
    if sys.platform == "win32":
        pytest.skip("Only relevant on non-Windows platforms")
    with pytest.raises(RuntimeError):
        win_helpers.unprotect_data(b"protected")


class _DATA_BLOB(ctypes.Structure):  # noqa: N801
    """Replica of the Windows DATA_BLOB structure used by DPAPI helpers."""

    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _make_mock_windll() -> MagicMock:
    """Build a fake ctypes.windll with kernel32 and crypt32 namespaces."""
    windll = MagicMock()
    windll.kernel32.RtlSecureZeroMemory = MagicMock()
    windll.kernel32.LocalFree = MagicMock()
    windll.crypt32.CryptProtectData = MagicMock()
    windll.crypt32.CryptUnprotectData = MagicMock()
    return windll


def _fill_blob(blob_out_ref: object, data: bytes) -> None:
    """Populate a DATA_BLOB output pointer with *data* for mock crypt32 calls."""
    blob_out = ctypes.cast(blob_out_ref, ctypes.POINTER(_DATA_BLOB)).contents
    buf = ctypes.create_string_buffer(data)
    blob_out.cbData = len(data)
    blob_out.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))


def _patch_ctypes(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace win_helpers.ctypes with a fake module that exposes windll."""
    mock_ctypes = types.ModuleType("ctypes")
    for name in dir(ctypes):
        if not name.startswith("__"):
            setattr(mock_ctypes, name, getattr(ctypes, name))
    mock_ctypes.windll = _make_mock_windll()
    monkeypatch.setattr("doctoragent.security.win_helpers.ctypes", mock_ctypes)
    monkeypatch.setattr("doctoragent.security.win_helpers.sys.platform", "win32")
    return mock_ctypes.windll


def test_secure_zero_with_mocked_windows_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    """secure_zero zeros the buffer via RtlSecureZeroMemory on Windows."""
    if sys.platform == "win32":
        pytest.skip("Uses mocked Windows APIs")
    windll = _patch_ctypes(monkeypatch)

    buf = bytearray(b"hello world")
    win_helpers.secure_zero(buf)

    windll.kernel32.RtlSecureZeroMemory.assert_called_once()
    assert buf == bytearray(len(buf))


def test_protect_data_with_mocked_windows_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    """protect_data returns the protected bytes from CryptProtectData."""
    if sys.platform == "win32":
        pytest.skip("Uses mocked Windows APIs")
    windll = _patch_ctypes(monkeypatch)

    def side_effect(*args: object, **kwargs: object) -> bool:
        blob_out_ref = args[6]
        _fill_blob(blob_out_ref, b"protected-blob")
        return True

    windll.crypt32.CryptProtectData.side_effect = side_effect

    result = win_helpers.protect_data(b"plain-text")

    assert result == b"protected-blob"
    windll.crypt32.CryptProtectData.assert_called_once()
    windll.kernel32.LocalFree.assert_called_once()


def test_protect_data_failure_with_mocked_windows_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    """protect_data raises RuntimeError when CryptProtectData returns false."""
    if sys.platform == "win32":
        pytest.skip("Uses mocked Windows APIs")
    windll = _patch_ctypes(monkeypatch)
    windll.crypt32.CryptProtectData.return_value = False

    with pytest.raises(RuntimeError, match="CryptProtectData failed"):
        win_helpers.protect_data(b"plain-text")


def test_unprotect_data_with_mocked_windows_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    """unprotect_data returns the original bytes from CryptUnprotectData."""
    if sys.platform == "win32":
        pytest.skip("Uses mocked Windows APIs")
    windll = _patch_ctypes(monkeypatch)

    def side_effect(*args: object, **kwargs: object) -> bool:
        blob_out_ref = args[6]
        _fill_blob(blob_out_ref, b"plain-text")
        return True

    windll.crypt32.CryptUnprotectData.side_effect = side_effect

    result = win_helpers.unprotect_data(b"protected-blob")

    assert result == b"plain-text"
    windll.crypt32.CryptUnprotectData.assert_called_once()
    windll.kernel32.LocalFree.assert_called_once()


def test_unprotect_data_failure_with_mocked_windows_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    """unprotect_data raises RuntimeError when CryptUnprotectData returns false."""
    if sys.platform == "win32":
        pytest.skip("Uses mocked Windows APIs")
    windll = _patch_ctypes(monkeypatch)
    windll.crypt32.CryptUnprotectData.return_value = False

    with pytest.raises(RuntimeError, match="CryptUnprotectData failed"):
        win_helpers.unprotect_data(b"protected-blob")
