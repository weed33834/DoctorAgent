"""Tests for the (now-removed) internal JSON-RPC style protocol.

``JsonRpcRequest`` and ``JsonRpcResponse`` were only used by tests and never
wired into production code, so the module was emptied. These tests verify the
module still imports cleanly and exposes no protocol symbols.
"""

import importlib


def test_protocol_module_imports_cleanly() -> None:
    """``doctoragent.api.protocol`` is importable as an empty placeholder module."""
    mod = importlib.import_module("doctoragent.api.protocol")
    assert mod is not None
    # The placeholder module exposes no protocol symbols.
    assert not hasattr(mod, "JsonRpcRequest")
    assert not hasattr(mod, "JsonRpcResponse")


def test_protocol_module_all_is_empty() -> None:
    """The placeholder module exposes an empty ``__all__``."""
    mod = importlib.import_module("doctoragent.api.protocol")
    assert getattr(mod, "__all__", None) == []


def test_api_package_no_longer_exports_protocol_classes() -> None:
    """``doctoragent.api`` re-exports schemas but not the removed JSON-RPC classes."""
    import doctoragent.api as api_pkg

    assert not hasattr(api_pkg, "JsonRpcRequest")
    assert not hasattr(api_pkg, "JsonRpcResponse")
    assert "JsonRpcRequest" not in getattr(api_pkg, "__all__", [])
    assert "JsonRpcResponse" not in getattr(api_pkg, "__all__", [])


def test_schemas_still_importable_from_api_package() -> None:
    """Removing the protocol module did not break schema re-exports."""
    from doctoragent.api import SearchQuery, SearchResult

    assert SearchQuery is not None
    assert SearchResult is not None
