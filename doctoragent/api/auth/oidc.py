"""OIDC / OAuth2 single-sign-on authentication for the DoctorAgent API.

Wraps `authlib <https://authlib.org/>`_ to verify bearer JWT access tokens
issued by an external OIDC provider. The authenticator fetches the provider's
JWKS via its discovery document, validates the token signature + claims, and
returns a normalised :class:`UserInfo` whose ``roles`` are mapped onto the
local :class:`~doctoragent.api.auth.rbac.Role` enum.

authlib is an optional dependency (the ``auth`` extra). When it is not
installed, constructing :class:`OIDCAuthenticator` raises :class:`ImportError`
with a clear install hint; the API server uses this to return ``503`` on
OIDC-protected endpoints instead of failing to start.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

from doctoragent.api.auth.rbac import Role

logger = logging.getLogger(__name__)


# ── Optional authlib import ──────────────────────────────────────────────────
#
# authlib's ``authlib.jose`` module emits a deprecation warning on import but
# remains the documented JWT verification entry point through 2.x. Import it
# lazily inside :meth:`OIDCAuthenticator._import_authlib` so that simply
# importing this module never requires authlib (or triggers its warning).

_AUTHLIB_AVAILABLE: bool | None = None


def _is_authlib_available() -> bool:
    """Return True if authlib's JWT primitives can be imported."""
    global _AUTHLIB_AVAILABLE
    if _AUTHLIB_AVAILABLE is not None:
        return _AUTHLIB_AVAILABLE
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from authlib.jose import JsonWebKey, jwt  # noqa: F401
            from authlib.jose.errors import JoseError  # noqa: F401

        _AUTHLIB_AVAILABLE = True
    except ImportError:
        _AUTHLIB_AVAILABLE = False
    return _AUTHLIB_AVAILABLE


# ── UserInfo model ───────────────────────────────────────────────────────────


class UserInfo(BaseModel):
    """Normalised authenticated-user identity."""

    sub: str
    email: str | None = None
    name: str | None = None
    roles: list[str] = Field(default_factory=list)


# ── Authenticator ────────────────────────────────────────────────────────────


class OIDCAuthenticator:
    """Verify OIDC bearer tokens and return normalised :class:`UserInfo`.

    Parameters
    ----------
    issuer_url:
        Base URL of the OIDC provider (e.g. ``https://accounts.example.com``).
        The discovery document is fetched from
        ``<issuer_url>/.well-known/openid-configuration``.
    client_id:
        Expected ``aud`` (audience) claim. May be empty when the provider
        does not issue audience-restricted tokens.
    client_secret:
        Reserved for future confidential-client flows (not used for pure
        bearer-token verification).
    audience:
        Optional explicit audience to validate. When provided it overrides
        *client_id* as the required ``aud`` value.
    """

    def __init__(
        self,
        issuer_url: str,
        client_id: str = "",
        client_secret: str | None = None,
        audience: str | None = None,
    ) -> None:
        if not issuer_url:
            raise ValueError("issuer_url is required for OIDCAuthenticator")
        self.issuer_url = issuer_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.audience = audience or client_id or None

        if not _is_authlib_available():
            raise ImportError(
                "authlib is required for OIDC authentication. "
                "Install it with: pip install 'doctoragent[auth]'"
            )

        # Lazily-populated caches.
        self._discovery: dict[str, Any] | None = None
        self._jwks: Any | None = None

    # ── authlib plumbing ────────────────────────────────────────────────

    def _import_authlib(self) -> tuple[Any, Any, Any]:
        """Return ``(jwt, JsonWebKey, JoseError)`` from authlib."""
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from authlib.jose import JsonWebKey, jwt
            from authlib.jose.errors import JoseError

        return jwt, JsonWebKey, JoseError

    # ── discovery + JWKS ────────────────────────────────────────────────

    def fetch_discovery(self) -> dict[str, Any]:
        """Fetch (and cache) the OIDC discovery document."""
        if self._discovery is not None:
            return self._discovery
        url = f"{self.issuer_url}/.well-known/openid-configuration"
        try:
            resp = httpx.get(url, timeout=10.0, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to fetch OIDC discovery from {url}: {exc}") from exc
        if not isinstance(data, dict) or "jwks_uri" not in data:
            raise RuntimeError(f"OIDC discovery at {url} is missing jwks_uri")
        self._discovery = data
        return data

    def fetch_jwks(self) -> Any:
        """Fetch (and cache) the provider's JWKS as an authlib key set."""
        if self._jwks is not None:
            return self._jwks
        _, JsonWebKey, _ = self._import_authlib()  # noqa: N806
        discovery = self.fetch_discovery()
        jwks_uri = discovery["jwks_uri"]
        try:
            resp = httpx.get(jwks_uri, timeout=10.0, follow_redirects=True)
            resp.raise_for_status()
            jwks_dict = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to fetch JWKS from {jwks_uri}: {exc}") from exc
        self._jwks = JsonWebKey.import_key_set(jwks_dict)
        return self._jwks

    # ── token verification ──────────────────────────────────────────────

    def decode_verify_token(self, token: str) -> dict[str, Any]:
        """Verify *token*'s signature and claims; return the claims dict.

        Raises :class:`RuntimeError` (or authlib's ``JoseError`` subclass) on
        any verification failure.
        """
        jwt, _, JoseError = self._import_authlib()  # noqa: N806
        key_set = self.fetch_jwks()

        claims_options: dict[str, Any] = {
            "iss": {"essential": True, "value": self.issuer_url},
        }
        if self.audience:
            claims_options["aud"] = {"essential": True, "value": self.audience}

        try:
            claims = jwt.decode(
                token,
                key_set,
                claims_options=claims_options,
            )
            claims.validate()
        except JoseError as exc:
            raise RuntimeError(f"OIDC token verification failed: {exc}") from exc
        except Exception as exc:
            # Defensive: surface any non-jose failure with context.
            raise RuntimeError(f"OIDC token verification error: {exc}") from exc

        return dict(claims)

    # ── role mapping ────────────────────────────────────────────────────

    @staticmethod
    def _map_roles(claims: dict[str, Any]) -> list[str]:
        """Map OIDC claims onto local :class:`Role` values.

        Recognised claim shapes (checked in order):
        * ``roles`` — list of strings
        * ``role`` — single string
        * ``realm_access.roles`` — Keycloak-style nested list

        Each value is lower-cased and kept only if it matches a local Role.
        When no recognised role claim is present the user defaults to
        ``viewer`` (least-privilege).
        """
        raw_roles: list[str] = []

        roles_claim = claims.get("roles")
        if isinstance(roles_claim, list):
            raw_roles.extend(str(r) for r in roles_claim if r)
        elif isinstance(roles_claim, str) and roles_claim:
            raw_roles.append(roles_claim)

        role_claim = claims.get("role")
        if isinstance(role_claim, str) and role_claim:
            raw_roles.append(role_claim)
        elif isinstance(role_claim, list):
            raw_roles.extend(str(r) for r in role_claim if r)

        realm_access = claims.get("realm_access")
        if isinstance(realm_access, dict):
            realm_roles = realm_access.get("roles")
            if isinstance(realm_roles, list):
                raw_roles.extend(str(r) for r in realm_roles if r)

        valid = {r.value for r in Role}
        mapped = [str(r).strip().lower() for r in raw_roles]
        mapped = [r for r in mapped if r in valid]
        # De-duplicate while preserving order.
        seen: set[str] = set()
        mapped = [r for r in mapped if not (r in seen or seen.add(r))]
        if not mapped:
            mapped = [Role.VIEWER.value]
        return mapped

    # ── request-level authentication ────────────────────────────────────

    async def authenticate(self, request: Any) -> UserInfo:
        """Extract the bearer token from *request*, verify it, return UserInfo.

        Raises :class:`RuntimeError` when the Authorization header is missing
        or malformed, or when token verification fails.
        """
        token = self._extract_bearer_token(request)
        if not token:
            raise RuntimeError("Missing bearer token in Authorization header")
        claims = self.decode_verify_token(token)
        return self._claims_to_userinfo(claims)

    @staticmethod
    def _extract_bearer_token(request: Any) -> str | None:
        """Pull the raw bearer token from the ``Authorization`` header.

        Delegates to the shared :func:`doctoragent.api.auth._guards.extract_bearer`
        so there is a single canonical bearer-extraction implementation
        across the API server, the advanced router and the OIDC path.
        """
        from doctoragent.api.auth._guards import extract_bearer

        return extract_bearer(request)

    def _claims_to_userinfo(self, claims: dict[str, Any]) -> UserInfo:
        sub = str(claims.get("sub") or claims.get("user_id") or "")
        if not sub:
            raise RuntimeError("OIDC token missing 'sub' claim")
        return UserInfo(
            sub=sub,
            email=claims.get("email"),
            name=claims.get("name") or claims.get("preferred_username"),
            roles=self._map_roles(claims),
        )


__all__ = ["OIDCAuthenticator", "UserInfo"]
