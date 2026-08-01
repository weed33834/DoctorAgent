"""Shared primitives for the bearer-token / local-only auth guards.

The API server (``doctoragent.api.server``), the advanced enterprise router
(``doctoragent.api.advanced_routes``) and the CDS Hooks router
(``doctoragent.clinical.integrations.cds_hooks.router``) all enforce the same
authentication policy:

* When ``DOCTORAGENT_OIDC_ISSUER`` is set the bearer token MUST be a valid
  OIDC JWT (delegated to :func:`doctoragent.api.server._authenticate_oidc`).
* Otherwise, when ``DOCTORAGENT_API_TOKEN`` is set, a constant-time bearer
  comparison is used.
* When neither is set, only loopback / Unix-socket callers are accepted
  (read endpoints) or the request is denied (sensitive endpoints).

Previously each router re-declared its own copy of the loopback set, the
token resolver, the OIDC-configured flag and the bearer extractor — four
divergent implementations of the same logic. This module is the single
source of truth so the policy cannot drift between surfaces.

The module is intentionally dependency-free (stdlib only) so it can be
imported by any router without pulling FastAPI at import time. The optional
``HTTPBearer`` scheme is constructed lazily by callers that have FastAPI.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

__all__ = [
    "LOCAL_HOSTS",
    "extract_bearer",
    "is_local_request",
    "oidc_is_configured",
    "resolve_token",
    "verify_bearer",
]

# Loopback addresses trusted for read endpoints when no token is configured.
# Unix-socket connections (``request.client is None``) are also trusted.
LOCAL_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


def resolve_token() -> str | None:
    """Resolve the expected static bearer token from the environment."""
    return os.environ.get("DOCTORAGENT_API_TOKEN")


def oidc_is_configured() -> bool:
    """Return ``True`` when the operator has enabled OIDC via env config."""
    return bool(os.environ.get("DOCTORAGENT_OIDC_ISSUER"))


def is_local_request(request: Any) -> bool:
    """Return ``True`` when *request* originates from a loopback address.

    Unix-socket connections (``request.client`` is ``None``) are treated as
    local — they can only originate from the same host.
    """
    client = getattr(request, "client", None)
    if client is None:
        return True
    return getattr(client, "host", None) in LOCAL_HOSTS


def extract_bearer(request: Any) -> str | None:
    """Pull the raw bearer token out of the ``Authorization`` header.

    Returns ``None`` when the header is absent or not a ``Bearer`` scheme.
    Robust against missing headers and case variants (``Authorization`` /
    ``authorization``) — this is the canonical implementation; routers should
    not re-implement it.
    """
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    try:
        auth_header = headers.get("Authorization") or headers.get("authorization")
    except AttributeError:
        return None
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def verify_bearer(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of a provided bearer token against *expected*.

    Returns ``True`` when *provided* matches *expected*, ``False`` otherwise
    (including when *provided* is ``None``). Callers that want to raise an
    ``HTTPException`` on mismatch should build their own error; this helper
    stays free of FastAPI so it can be unit-tested without the web layer.
    """
    if not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())
