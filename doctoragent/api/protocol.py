"""JSON-RPC 2.0 style internal protocol (removed).

This module previously hosted ``JsonRpcRequest`` / ``JsonRpcResponse``
envelopes, but they were only exercised by tests and never used in
production code. The classes have been removed. The file is kept as an
empty placeholder so that older ``import doctoragent.api.protocol`` calls do
not raise ``ImportError``; new code should use the Pydantic schemas in
``doctoragent.api.schemas`` instead.
"""

__all__: list[str] = []
