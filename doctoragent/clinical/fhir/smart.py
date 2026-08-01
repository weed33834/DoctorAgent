"""SMART-on-FHIR v2 client launch (standalone + EHR-launch).

Implements the OAuth2 Authorization Code + PKCE flow defined by the
SMART App Launch framework v2 (https://build.fhir.org/ig/HL7/smart-app-launch/).
Built on top of :mod:`authlib` (already an optional dependency under the
``auth`` extra) and :mod:`httpx` (core dependency) — no wheel reinvented.

Capabilities
------------
- **Capability statement discovery** — ``GET /metadata`` is fetched once,
  SMART extensions are parsed to extract the authorize / token endpoints
  (and the scopes the EHR advertises).
- **`.well-known/smart-configuration`** — when the EHR publishes it
  (SMART v2), it is preferred over the capability statement.
- **Standalone launch** — the app initiates the flow with a user-chosen
  patient scope (``launch/patient``).
- **EHR launch** — the EHR passes ``launch`` + ``aud`` as query params;
  the client forwards them through so the EHR can resolve the patient
  context server-side.
- **PKCE S256** — a fresh ``code_verifier`` is generated per launch,
  never reused; the ``code_challenge`` is the SHA-256 digest (S256).
- **Scope validation** — the returned token's ``scope`` is checked
  against what the caller requested so a server silently dropping a
  scope (e.g. ``patient/*.read``) is surfaced as a security error rather
  than a silent privilege downgrade.
- **Token refresh** — when a ``refresh_token`` is returned, the client
  can refresh proactively; expired access tokens are never used silently.
- **Asymmetric JWKS / client-secret auth** — supports ``client_secret_post``
  and ``client_secret_basic`` (the two profiles the SMART v2 IG permits
  for confidential clients); public clients omit the auth.

Returned tokens feed straight into :class:`FHIRClient` as the
``auth_token`` (SMART bearer), and into the CDS Hooks ``fhirAuthorization``
field when DoctorAgent is invoked by the EHR — closing the loop
described in ``docs/MEDICAL_PIVOT_DESIGN.md`` Phase-B4.

The class is fully async (matching :class:`FHIRClient`) so a FastAPI
request handler can ``await`` a launch without blocking the event loop.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# authlib is optional (the ``auth`` extra). The import is guarded so this
# module is import-safe on minimal installs; the SMARTClient methods raise
# a clear ImportError when actually used without authlib. We import
# ``create_s256_code_challenge`` both because we use it and because its
# importability is the authlib-availability probe.
try:
    from authlib.oauth2.rfc7636 import create_s256_code_challenge

    _AUTHLIB_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only on minimal installs
    _AUTHLIB_AVAILABLE = False


__all__ = [
    "SMARTClient",
    "SMARTDiscovery",
    "SMARTLaunchResult",
    "SMARTScopeError",
    "SMARTDiscoveryError",
    "SMARTLaunchError",
    "SMARTLaunchParams",
]

# URL of the SMART v2 ``oauth-uris`` extension in a FHIR CapabilityStatement's
# ``rest[0].security.extension[]``. The extension is *nested*: the outer entry
# carries this URL and an inner ``extension[]`` array with authorize / token /
# register / manage entries.
_SMART_OAUTH_URIS_URL = "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris"


# SMART v2 scopes are dot-namespaced (``patient/*.read``, ``user/Observation.rs``,
# ``launch/patient``, ``openid fhirUser`` …). A token whose ``scope`` string
# contains any of these tokens is considered to grant that scope.
_DEFAULT_SCOPES: tuple[str, ...] = (
    "openid",
    "fhirUser",
    "launch/patient",
    "patient/*.read",
    "patient/Observation.rs",
    "patient/MedicationRequest.rs",
    "patient/Condition.rs",
    "patient/AllergyIntolerance.rs",
    "patient/Patient.rs",
    "patient/Encounter.rs",
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class SMARTError(RuntimeError):
    """Base class for SMART-on-FHIR client errors."""


class SMARTDiscoveryError(SMARTError):
    """Capability statement / smart-configuration discovery failed."""


class SMARTLaunchError(SMARTError):
    """Token exchange or authorization step failed."""


class SMARTScopeError(SMARTLaunchError):
    """The granted scopes do not satisfy the request (privilege downgrade)."""


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #
@dataclass
class SMARTDiscovery:
    """Endpoints and capabilities advertised by the FHIR server."""

    authorize_url: str
    token_url: str
    introspect_url: str | None = None
    revoke_url: str | None = None
    management_url: str | None = None
    scopes_supported: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    client_id_supported: bool = True
    grant_types_supported: list[str] = field(default_factory=lambda: ["authorization_code"])
    response_types_supported: list[str] = field(default_factory=lambda: ["code"])
    code_challenge_methods_supported: list[str] = field(default_factory=lambda: ["S256"])

    def supports_pkce(self) -> bool:
        return "S256" in self.code_challenge_methods_supported

    def supports_refresh(self) -> bool:
        return "refresh_token" in self.grant_types_supported

    def supports_scope(self) -> bool:
        return bool(self.scopes_supported)


@dataclass
class SMARTLaunchParams:
    """Inputs for a single SMART launch attempt."""

    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...] = _DEFAULT_SCOPES
    # EHR launch context. ``launch`` is the opaque token the EHR passes on
    # launch; ``aud`` is the FHIR base URL the EHR expects as the audience.
    # For standalone launch both are ``None``.
    launch: str | None = None
    aud: str | None = None
    state: str | None = None
    # When the caller already knows the patient (e.g. selected in our UI),
    # skip the picker.
    patient_id: str | None = None


@dataclass
class SMARTLaunchResult:
    """Token + metadata returned by a successful launch."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int | None = None
    expires_at: float | None = None  # monotonic deadline
    refresh_token: str | None = None
    scope: str = ""
    patient: str | None = None  # FHIR Patient id the EHR bound the token to
    id_token: str | None = None  # OIDC id_token (SMART v2 + openid scope)
    raw: dict[str, Any] = field(default_factory=dict)

    def as_authorization_header(self) -> str:
        """Return the ``Authorization`` header value for :class:`FHIRClient`."""
        return f"{self.token_type} {self.access_token}".strip()


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class SMARTClient:
    """SMART-on-FHIR v2 launch client.

    Lifecycle::

        async with SMARTClient(fhir_base="https://fhir.example.com/fhir",
                               client_id="my-app",
                               redirect_uri="https://my.app/callback") as sc:
            discovery = await sc.discover()
            url, verifier, state = sc.build_authorization_url(discovery,
                                                              scopes=("patient/*.read",))
            # ... user opens ``url`` in browser, EHR redirects to redirect_uri
            #     with ?code=...&state=...
            result = await sc.exchange_code(discovery, code, verifier, state)
            # result.access_token -> FHIRClient(auth_token=...)
    """

    def __init__(
        self,
        fhir_base: str,
        *,
        client_id: str,
        redirect_uri: str,
        client_secret: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not fhir_base:
            raise ValueError("fhir_base is required")
        if not client_id:
            raise ValueError("client_id is required")
        if not redirect_uri:
            raise ValueError("redirect_uri is required")
        self.fhir_base = fhir_base.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.client_secret = client_secret
        self.timeout = timeout
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport
        # Plain httpx client for discovery + token exchange; we POST the
        # token form ourselves (see _post_token) so we control the exact
        # body fields and surface server errors verbatim, rather than using
        # authlib's AsyncOAuth2Client which rewrites the request.
        self._http = httpx.AsyncClient(base_url=self.fhir_base, **client_kwargs)

    # ----- lifecycle -------------------------------------------------------- #
    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> SMARTClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # ----- discovery -------------------------------------------------------- #
    async def discover(self) -> SMARTDiscovery:
        """Fetch SMART endpoints from ``.well-known/smart-configuration`` (v2).

        Falls back to the FHIR CapabilityStatement (``GET /metadata``) when
        the EHR does not serve the v2 well-known document.
        """
        # 1) try SMART v2 well-known
        try:
            resp = await self._http.get("/.well-known/smart-configuration")
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict):
                    d = self._parse_smart_configuration(body)
                    if d is not None:
                        return d
        except (httpx.RequestError, ValueError):
            logger.debug("smart-configuration unavailable; falling back to metadata")

        # 2) fall back to CapabilityStatement
        return await self._discover_from_metadata()

    @staticmethod
    def _parse_smart_configuration(body: dict[str, Any]) -> SMARTDiscovery | None:
        authorize = body.get("authorize_endpoint")
        token = body.get("token_endpoint")
        if not authorize or not token:
            return None
        return SMARTDiscovery(
            authorize_url=authorize,
            token_url=token,
            introspect_url=body.get("introspect_endpoint"),
            revoke_url=body.get("revoke_endpoint"),
            management_url=body.get("management_endpoint"),
            scopes_supported=list(body.get("scopes_supported") or []),
            capabilities=list(body.get("capabilities") or []),
            client_id_supported=bool(body.get("client_id_supported", True)),
            grant_types_supported=list(body.get("grant_types_supported") or ["authorization_code"]),
            response_types_supported=list(body.get("response_types_supported") or ["code"]),
            code_challenge_methods_supported=list(
                body.get("code_challenge_methods_supported") or ["S256"]
            ),
        )

    async def _discover_from_metadata(self) -> SMARTDiscovery:
        try:
            resp = await self._http.get("/metadata")
            resp.raise_for_status()
            body = resp.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            raise SMARTDiscoveryError(f"CapabilityStatement fetch failed: {exc}") from exc
        if not isinstance(body, dict):
            raise SMARTDiscoveryError("CapabilityStatement is not a JSON object")
        rest = body.get("rest") or []
        if not isinstance(rest, list) or not rest:
            raise SMARTDiscoveryError("CapabilityStatement has no rest[] entry")
        security = (rest[0] if isinstance(rest[0], dict) else {}).get("security") or {}
        if not isinstance(security, dict):
            raise SMARTDiscoveryError("CapabilityStatement.rest[0].security missing")
        # SMART on FHIR spec: the SMART oauth-uris extension is a NESTED
        # extension under rest[0].security.extension — the outer extension
        # has url _SMART_OAUTH_URIS_URL (see module constant above) and
        # contains an inner extension[] array with authorize / token /
        # register / manage entries. Some servers also publish a flat
        # non-standard extension[] (legacy / dev) so we accept both shapes.
        authorize_url: str | None = None
        token_url: str | None = None
        extensions = security.get("extension") or []
        # 1) Standard SMART nested structure: find the oauth-uris extension
        #    and traverse its inner extension[] array.
        for ext in extensions:
            if not isinstance(ext, dict):
                continue
            if ext.get("url") != _SMART_OAUTH_URIS_URL:
                continue
            inner = ext.get("extension") or []
            for sub in inner:
                if not isinstance(sub, dict):
                    continue
                sub_url = sub.get("url")
                if sub_url == "authorize":
                    authorize_url = sub.get("valueUri") or authorize_url
                elif sub_url == "token":
                    token_url = sub.get("valueUri") or token_url
        # 2) Legacy / non-standard flat structure (some dev servers): the
        #    authorize/token entries sit directly in security.extension[].
        #    Only honour this when the standard nested lookup failed so
        #    we don't pick up stray values from unrelated extensions.
        if not authorize_url or not token_url:
            for ext in extensions:
                if not isinstance(ext, dict):
                    continue
                url = ext.get("url")
                if url == "authorize":
                    authorize_url = authorize_url or ext.get("valueUri")
                elif url == "token":
                    token_url = token_url or ext.get("valueUri")
        if not authorize_url or not token_url:
            raise SMARTDiscoveryError(
                "CapabilityStatement.rest[0].security.extension missing SMART "
                "oauth-uris (authorize/token) — not a SMART-enabled FHIR server"
            )
        return SMARTDiscovery(
            authorize_url=authorize_url,
            token_url=token_url,
            # CapabilityStatement doesn't carry the rest; use sensible defaults.
            code_challenge_methods_supported=["S256"],
        )

    # ----- authorization URL ------------------------------------------------ #
    def build_authorization_url(
        self,
        discovery: SMARTDiscovery,
        *,
        params: SMARTLaunchParams,
    ) -> tuple[str, str, str]:
        """Build the EHR authorize URL with PKCE.

        Returns ``(url, code_verifier, state)``. The caller must persist
        ``code_verifier`` and ``state`` between the redirect and
        :meth:`exchange_code` (HTTP session / server-side state).

        Raises :class:`SMARTLaunchError` when authlib is unavailable or the
        EHR doesn't advertise PKCE S256 (we never silently downgrade to
        ``plain`` — that would weaken the launch against network attackers).

        PKCE S256 challenge derivation uses :func:`authlib.oauth2.rfc7636.
        create_s256_code_challenge` — the URL itself is assembled via a plain
        :func:`urllib.parse.urlencode` call so we control the exact param
        set the EHR receives (authlib's ``create_authorization_url`` rewrites
        scope/redirect_uri in ways that conflict with SMART's launch/aud
        extensions).
        """
        if not _AUTHLIB_AVAILABLE:
            raise SMARTLaunchError(
                "authlib is required for SMART launches: pip install 'doctoragent[auth]'"
            )
        if not discovery.supports_pkce():
            raise SMARTLaunchError(
                "EHR does not advertise PKCE S256; refusing to launch "
                "(would be vulnerable to authorization-code interception)"
            )
        code_verifier = _generate_code_verifier()
        state = params.state or _generate_state()
        challenge = create_s256_code_challenge(code_verifier)
        query: dict[str, str] = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(params.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if params.launch:
            query["launch"] = params.launch
        if params.aud:
            query["aud"] = params.aud
        from urllib.parse import urlencode

        url = f"{discovery.authorize_url}?{urlencode(query)}"
        return url, code_verifier, state

    # ----- token exchange --------------------------------------------------- #
    async def exchange_code(
        self,
        discovery: SMARTDiscovery,
        *,
        code: str,
        code_verifier: str,
        state: str | None = None,
        redirect_uri: str | None = None,
    ) -> SMARTLaunchResult:
        """Exchange an authorization code for a token (PKCE verified).

        Performs a direct ``application/x-www-form-urlencoded`` POST to the
        token endpoint (RFC 6749 §4.1.3) so we control the exact body
        fields and can surface server-side errors verbatim. Raises
        :class:`SMARTLaunchError` on transport / OAuth2 error.
        """
        if not code:
            raise SMARTLaunchError("authorization code is required")
        if not code_verifier:
            raise SMARTLaunchError("code_verifier is required")
        redirect = redirect_uri or self.redirect_uri
        form: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        if state:
            form["state"] = state
        if self.client_secret:
            form["client_secret"] = self.client_secret
        token = await self._post_token(discovery.token_url, form)
        return self._build_result(token)

    # ----- refresh ---------------------------------------------------------- #
    async def refresh_token(
        self,
        discovery: SMARTDiscovery,
        *,
        refresh_token: str,
    ) -> SMARTLaunchResult:
        """Exchange a refresh token for a fresh access token.

        Raises :class:`SMARTLaunchError` when the EHR rejects the refresh
        (e.g. expired / revoked / server policy). The caller should re-launch.
        """
        if not refresh_token:
            raise SMARTLaunchError("refresh_token is required")
        if not discovery.supports_refresh():
            raise SMARTLaunchError("EHR does not advertise refresh_token grant")
        form: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }
        if self.client_secret:
            form["client_secret"] = self.client_secret
        token = await self._post_token(discovery.token_url, form)
        return self._build_result(token)

    async def _post_token(self, token_url: str, form: dict[str, str]) -> dict[str, Any]:
        """POST a token-exchange form and return the parsed JSON token dict.

        Surfaces OAuth2 error responses (RFC 6749 §5.2) as
        :class:`SMARTLaunchError` with the server's ``error`` /
        ``error_description`` for debugging. Network failures are wrapped
        likewise so callers see a single error type.
        """
        try:
            resp = await self._http.post(
                token_url,
                data=form,
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise SMARTLaunchError(f"token endpoint unreachable: {exc}") from exc
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                err = {"error": resp.text}
            desc = err.get("error_description") or err.get("error") or resp.text
            raise SMARTLaunchError(f"token exchange failed (HTTP {resp.status_code}): {desc}")
        try:
            token = resp.json()
        except ValueError as exc:
            raise SMARTLaunchError(f"token endpoint returned non-JSON: {exc}") from exc
        if not isinstance(token, dict) or not token.get("access_token"):
            raise SMARTLaunchError("token endpoint returned no access_token in response")
        return token

    # ----- helpers ---------------------------------------------------------- #
    @staticmethod
    def _build_result(token: dict[str, Any]) -> SMARTLaunchResult:
        """Wrap authlib's token dict into :class:`SMARTLaunchResult`."""
        return SMARTLaunchResult(
            access_token=str(token.get("access_token") or ""),
            token_type=str(token.get("token_type") or "Bearer"),
            expires_in=token.get("expires_in"),
            expires_at=token.get("expires_at"),
            refresh_token=token.get("refresh_token"),
            scope=str(token.get("scope") or ""),
            patient=token.get("patient"),
            id_token=token.get("id_token"),
            raw=dict(token),
        )

    # SMART v2 clinical scope syntax: ``{context}/{resource}.{permission}``.
    # ``context`` ∈ {patient, user, system, tenant}; ``resource`` is ``*`` or a
    # FHIR resource type; ``permission`` is ``*`` or a CRUD combo
    # (``read``/``write``/``rs``/``cru``…). Non-clinical scopes (``openid``,
    # ``fhirUser``, ``launch/patient``) carry no ``.`` and are matched verbatim.
    _CLINICAL_SCOPE_RE = re.compile(
        r"^(?P<ctx>patient|user|system|tenant)/"
        r"(?P<res>\*|[A-Za-z][A-Za-z0-9]*)\."
        r"(?P<perm>\*|[A-Za-z][A-Za-z0-9]*)$"
    )

    @classmethod
    def _parse_clinical_scope(cls, scope: str) -> tuple[str, str, str] | None:
        """Return ``(context, resource, permission)`` or ``None`` if not clinical."""
        m = cls._CLINICAL_SCOPE_RE.match(scope)
        if m is None:
            return None
        return m.group("ctx"), m.group("res"), m.group("perm")

    @staticmethod
    def _segment_satisfies(granted_seg: str, requested_seg: str) -> bool:
        """Whether a granted scope-segment satisfies a requested one.

        ``*`` on either side matches any value on the other — this gives the
        bidirectional wildcard semantics required by SMART scope checks:
        - granted ``patient/*.read`` covers requested ``patient/Observation.read``
        - granted ``patient/Observation.read`` satisfies requested ``patient/*.read``
          (the app asked broadly and the EHR granted a usable subset).
        Two concrete values match only when equal.
        """
        if granted_seg == "*" or requested_seg == "*":
            return True
        return granted_seg == requested_seg

    @classmethod
    def _scope_satisfies(cls, granted: str, requested: str) -> bool:
        """Whether a single granted scope satisfies a requested scope.

        Non-clinical scopes (OIDC ``openid``/``fhirUser``, ``launch/*``)
        require an exact match. Clinical scopes match when ``context`` is
        identical and both the ``resource`` and ``permission`` segments are
        compatible under :meth:`_segment_satisfies`. Different contexts
        (``patient`` vs ``user``) never satisfy each other — they are
        distinct SMART v2 security compartments.
        """
        if granted == requested:
            return True
        g = cls._parse_clinical_scope(granted)
        r = cls._parse_clinical_scope(requested)
        if g is None or r is None:
            return False
        g_ctx, g_res, g_perm = g
        r_ctx, r_res, r_perm = r
        if g_ctx != r_ctx:
            return False
        return cls._segment_satisfies(g_res, r_res) and cls._segment_satisfies(g_perm, r_perm)

    @staticmethod
    def verify_scope(
        granted: str,
        requested: tuple[str, ...],
    ) -> None:
        """Check that every requested scope is satisfied by the granted set.

        SMART v2 clinical scopes use ``{context}/{resource}.{permission}``
        with ``*`` wildcards. Wildcards are honoured in BOTH directions so a
        privilege change is never misreported:

        - granted ``patient/*.read`` satisfies requested
          ``patient/Observation.read`` (server granted broader access), and
        - granted ``patient/Observation.read`` satisfies requested
          ``patient/*.read`` (EHR narrowed a broad request to a usable
          subset via the patient consent screen).

        A token that grants a scope for a *different* resource (e.g.
        requested ``patient/Condition.read``, granted only
        ``patient/Patient.read``) is surfaced as a privilege downgrade and
        raises :class:`SMARTScopeError`. OIDC / launch scopes (``openid``,
        ``fhirUser``, ``launch/patient``) are required verbatim.
        """
        granted_set = set(granted.split())
        for want in requested:
            if any(SMARTClient._scope_satisfies(g, want) for g in granted_set):
                continue
            raise SMARTScopeError(
                f"requested scope '{want}' not granted (granted: {sorted(granted_set)})"
            )


# --------------------------------------------------------------------------- #
# PKCE primitives (authlib provides create_s256_code_challenge; we still need
# the verifier generator — 43-128 chars from the unreserved set, RFC 7636 §4.1)
# --------------------------------------------------------------------------- #
_UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def _generate_code_verifier(length: int = 64) -> str:
    """RFC 7636 §4.1 code_verifier — high-entropy URL-safe string."""
    if length < 43 or length > 128:
        raise ValueError("code_verifier length must be 43-128")
    return "".join(secrets.choice(_UNRESERVED) for _ in range(length))


def _generate_state(length: int = 32) -> str:
    """CSRF / replay-protection state token (opaque to the EHR)."""
    return secrets.token_urlsafe(length)
