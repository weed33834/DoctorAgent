"""Tests for the SMART-on-FHIR v2 launch client.

Covers discovery (well-known + capability-statement fallback),
authorize-URL construction with PKCE, authorization-code exchange,
refresh, scope verification (incl. wildcard + downgrade detection),
and the error paths (no PKCE, transport failure, malformed discovery).
All network is mocked via ``httpx.MockTransport`` — no real EHR needed.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from doctoragent.clinical.fhir.smart import (
    SMARTClient,
    SMARTDiscovery,
    SMARTDiscoveryError,
    SMARTLaunchError,
    SMARTLaunchParams,
    SMARTScopeError,
)


# --------------------------------------------------------------------------- #
# Mock-transport builders
# --------------------------------------------------------------------------- #
def _smart_config_response(
    request: httpx.Request,
) -> httpx.Response:
    """Serve a SMART v2 ``.well-known/smart-configuration`` document."""
    return httpx.Response(
        200,
        json={
            "authorize_endpoint": "https://ehr.example.com/auth/authorize",
            "token_endpoint": "https://ehr.example.com/auth/token",
            "introspect_endpoint": "https://ehr.example.com/auth/introspect",
            "revoke_endpoint": "https://ehr.example.com/auth/revoke",
            "scopes_supported": ["patient/*.read", "launch/patient", "openid"],
            "capabilities": ["client-confidential-asymmetric", "launch-ehr"],
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
        },
    )


def _metadata_response(request: httpx.Request) -> httpx.Response:
    """Serve a FHIR CapabilityStatement with the standard SMART nested
    oauth-uris extension structure (the shape real EHRs like Cerner /
    Epic / Logica return).

    Spec: rest[0].security.extension[] contains an extension with
    url="http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris",
    whose nested extension[] holds authorize / token / register entries.
    """
    return httpx.Response(
        200,
        json={
            "resourceType": "CapabilityStatement",
            "rest": [
                {
                    "security": {
                        "extension": [
                            {
                                "url": (
                                    "http://fhir-registry.smarthealthit.org/"
                                    "StructureDefinition/oauth-uris"
                                ),
                                "extension": [
                                    {
                                        "url": "authorize",
                                        "valueUri": "https://ehr.example.com/auth/authorize",
                                    },
                                    {
                                        "url": "token",
                                        "valueUri": "https://ehr.example.com/auth/token",
                                    },
                                    {
                                        "url": "register",
                                        "valueUri": "https://ehr.example.com/auth/register",
                                    },
                                ],
                            }
                        ]
                    }
                }
            ],
        },
    )


def _token_response(request: httpx.Request) -> httpx.Response:
    """Return a successful token JSON.

    Accepts form-encoded token-exchange POSTs (``grant_type=authorization_code``
    or ``grant_type=refresh_token``) and returns a fixed SMART token.
    """
    return httpx.Response(
        200,
        json={
            "access_token": "smart-access-token-123",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "refresh-token-456",
            "scope": " ".join(
                [
                    "patient/Patient.read",
                    "patient/*.read",
                    "launch/patient",
                    "openid",
                    "fhirUser",
                ]
            ),
            "patient": "patient-42",
        },
    )


def _build_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> SMARTClient:
    return SMARTClient(
        fhir_base="https://ehr.example.com/fhir",
        client_id="doctoragent-app",
        redirect_uri="https://my.app/cb",
        transport=transport,
    )


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
class TestDiscovery:
    @pytest.mark.asyncio
    async def test_well_known_smart_config(self) -> None:
        transport = _build_transport(_smart_config_response)
        async with _client(transport) as sc:
            d = await sc.discover()
        assert d.authorize_url == "https://ehr.example.com/auth/authorize"
        assert d.token_url == "https://ehr.example.com/auth/token"
        assert "S256" in d.code_challenge_methods_supported
        assert d.supports_pkce() is True
        assert d.supports_refresh() is True
        assert "launch-ehr" in d.capabilities

    @pytest.mark.asyncio
    async def test_falls_back_to_metadata(self) -> None:
        # First request to /.well-known returns 404 → fallback to /metadata.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/.well-known/smart-configuration"):
                return httpx.Response(404)
            if request.url.path.endswith("/metadata"):
                return _metadata_response(request)
            return httpx.Response(500)

        transport = _build_transport(handler)
        async with _client(transport) as sc:
            d = await sc.discover()
        assert d.authorize_url == "https://ehr.example.com/auth/authorize"
        assert d.token_url == "https://ehr.example.com/auth/token"

    @pytest.mark.asyncio
    async def test_metadata_without_smart_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"resourceType": "CapabilityStatement", "rest": [{}]},
            )

        transport = _build_transport(handler)
        async with _client(transport) as sc:
            with pytest.raises(SMARTDiscoveryError, match="not a SMART-enabled"):
                await sc.discover()

    @pytest.mark.asyncio
    async def test_network_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns fail")

        transport = _build_transport(handler)
        async with _client(transport) as sc:
            with pytest.raises(SMARTDiscoveryError):
                await sc.discover()

    @pytest.mark.asyncio
    async def test_empty_base_url_rejected(self) -> None:
        with pytest.raises(ValueError, match="fhir_base"):
            SMARTClient(
                fhir_base="",
                client_id="x",
                redirect_uri="https://my.app/cb",
            )


# --------------------------------------------------------------------------- #
# Authorization URL
# --------------------------------------------------------------------------- #
class TestAuthorizationURL:
    @pytest.mark.asyncio
    async def test_authorize_url_contains_pkce_and_scopes(self) -> None:
        transport = _build_transport(_smart_config_response)
        async with _client(transport) as sc:
            d = await sc.discover()
            params = SMARTLaunchParams(
                client_id="doctoragent-app",
                redirect_uri="https://my.app/cb",
                scopes=("patient/*.read", "openid", "fhirUser"),
            )
            url, verifier, state = sc.build_authorization_url(d, params=params)
        assert verifier and 43 <= len(verifier) <= 128
        assert state and len(state) >= 16
        parsed = urlparse(url)
        q = parse_qs(parsed.query)
        assert parsed.netloc == "ehr.example.com"
        assert parsed.path == "/auth/authorize"
        assert q["client_id"] == ["doctoragent-app"]
        assert q["response_type"] == ["code"]
        assert q["redirect_uri"] == ["https://my.app/cb"]
        assert q["code_challenge_method"] == ["S256"]
        assert q["code_challenge"][0]  # populated
        assert q["state"][0] == state
        scope = q["scope"][0].split()
        assert "patient/*.read" in scope
        assert "openid" in scope
        assert "fhirUser" in scope

    @pytest.mark.asyncio
    async def test_ehr_launch_includes_launch_and_aud(self) -> None:
        transport = _build_transport(_smart_config_response)
        async with _client(transport) as sc:
            d = await sc.discover()
            params = SMARTLaunchParams(
                client_id="doctoragent-app",
                redirect_uri="https://my.app/cb",
                scopes=("launch/patient",),
                launch="opaque-ehr-launch-token",
                aud="https://ehr.example.com/fhir",
            )
            url, _, _ = sc.build_authorization_url(d, params=params)
        q = parse_qs(urlparse(url).query)
        assert q["launch"] == ["opaque-ehr-launch-token"]
        assert q["aud"] == ["https://ehr.example.com/fhir"]

    @pytest.mark.asyncio
    async def test_no_pkce_support_refuses(self) -> None:
        d = SMARTDiscovery(
            authorize_url="https://ehr.example.com/auth/authorize",
            token_url="https://ehr.example.com/auth/token",
            code_challenge_methods_supported=[],  # no S256
        )
        transport = _build_transport(lambda r: httpx.Response(404))
        async with _client(transport) as sc:
            params = SMARTLaunchParams(
                client_id="doctoragent-app",
                redirect_uri="https://my.app/cb",
            )
            with pytest.raises(SMARTLaunchError, match="PKCE S256"):
                sc.build_authorization_url(d, params=params)

    def test_build_url_no_authlib(self, monkeypatch) -> None:
        # Simulate authlib missing.
        from doctoragent.clinical.fhir import smart as smart_mod
        monkeypatch.setattr(smart_mod, "_AUTHLIB_AVAILABLE", False)
        sc = SMARTClient(
            fhir_base="https://ehr.example.com/fhir",
            client_id="x",
            redirect_uri="https://my.app/cb",
        )
        d = SMARTDiscovery(
            authorize_url="https://ehr.example.com/auth/authorize",
            token_url="https://ehr.example.com/auth/token",
        )
        with pytest.raises(SMARTLaunchError, match="authlib is required"):
            sc.build_authorization_url(
                d,
                params=SMARTLaunchParams(
                    client_id="x",
                    redirect_uri="https://my.app/cb",
                ),
            )


# --------------------------------------------------------------------------- #
# Code exchange
# --------------------------------------------------------------------------- #
class TestCodeExchange:
    @pytest.mark.asyncio
    async def test_exchange_code_returns_token(self) -> None:
        transport = _build_transport(_token_response)
        async with _client(transport) as sc:
            d = SMARTDiscovery(
                authorize_url="https://ehr.example.com/auth/authorize",
                token_url="https://ehr.example.com/auth/token",
            )
            result = await sc.exchange_code(
                d,
                code="auth-code-from-ehr",
                code_verifier="verifier-43-chars-min-xxxxxxxxxxxxxxxxxxxxxx",
                state="state-123",
            )
        assert result.access_token == "smart-access-token-123"
        assert result.token_type == "Bearer"
        assert result.expires_in == 3600
        assert result.refresh_token == "refresh-token-456"
        assert result.patient == "patient-42"
        header = result.as_authorization_header()
        assert header == "Bearer smart-access-token-123"

    @pytest.mark.asyncio
    async def test_exchange_code_empty_code_raises(self) -> None:
        transport = _build_transport(lambda r: httpx.Response(200, json={}))
        async with _client(transport) as sc:
            d = SMARTDiscovery(
                authorize_url="x",
                token_url="https://ehr.example.com/auth/token",
            )
            with pytest.raises(SMARTLaunchError, match="authorization code"):
                await sc.exchange_code(
                    d, code="", code_verifier="v", state="s"
                )

    @pytest.mark.asyncio
    async def test_exchange_code_server_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        transport = _build_transport(handler)
        async with _client(transport) as sc:
            d = SMARTDiscovery(
                authorize_url="x",
                token_url="https://ehr.example.com/auth/token",
            )
            with pytest.raises(SMARTLaunchError, match="token exchange failed"):
                await sc.exchange_code(
                    d, code="c", code_verifier="v", state="s"
                )

    @pytest.mark.asyncio
    async def test_exchange_with_client_secret(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content.decode()
            return _token_response(request)

        transport = _build_transport(handler)
        sc = SMARTClient(
            fhir_base="https://ehr.example.com/fhir",
            client_id="confidential-app",
            redirect_uri="https://my.app/cb",
            client_secret="super-secret",
            transport=transport,
        )
        async with sc:
            d = SMARTDiscovery(
                authorize_url="x",
                token_url="https://ehr.example.com/auth/token",
            )
            result = await sc.exchange_code(
                d, code="c", code_verifier="v", state="s"
            )
        assert result.access_token == "smart-access-token-123"
        # authlib encodes client_secret in the token POST body for
        # client_secret_post (default). The body must include it.
        assert "super-secret" in captured["body"]


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #
class TestRefreshToken:
    @pytest.mark.asyncio
    async def test_refresh_returns_new_token(self) -> None:
        transport = _build_transport(_token_response)
        async with _client(transport) as sc:
            d = SMARTDiscovery(
                authorize_url="x",
                token_url="https://ehr.example.com/auth/token",
                grant_types_supported=["authorization_code", "refresh_token"],
            )
            result = await sc.refresh_token(
                d, refresh_token="old-refresh"
            )
        assert result.access_token == "smart-access-token-123"

    @pytest.mark.asyncio
    async def test_refresh_unsupported_grant_raises(self) -> None:
        transport = _build_transport(_token_response)
        async with _client(transport) as sc:
            d = SMARTDiscovery(
                authorize_url="x",
                token_url="https://ehr.example.com/auth/token",
                grant_types_supported=["authorization_code"],  # no refresh
            )
            with pytest.raises(SMARTLaunchError, match="does not advertise"):
                await sc.refresh_token(d, refresh_token="x")

    @pytest.mark.asyncio
    async def test_refresh_empty_token_raises(self) -> None:
        transport = _build_transport(_token_response)
        async with _client(transport) as sc:
            d = SMARTDiscovery(
                authorize_url="x",
                token_url="https://ehr.example.com/auth/token",
                grant_types_supported=["authorization_code", "refresh_token"],
            )
            with pytest.raises(SMARTLaunchError, match="refresh_token is required"):
                await sc.refresh_token(d, refresh_token="")


# --------------------------------------------------------------------------- #
# Scope verification
# --------------------------------------------------------------------------- #
class TestScopeVerification:
    def test_exact_match_ok(self) -> None:
        SMARTClient.verify_scope(
            "patient/Patient.read openid",
            ("patient/Patient.read", "openid"),
        )

    def test_wildcard_match_ok(self) -> None:
        # ``patient/*.read`` should match any ``patient/X.read`` granted.
        SMARTClient.verify_scope(
            "patient/Observation.read patient/Patient.read",
            ("patient/*.read",),
        )

    def test_granted_wildcard_covers_requested_specific(self) -> None:
        # Direction B: granted ``patient/*.read`` (broad) must satisfy a
        # requested specific ``patient/Observation.read``. The old matcher
        # only handled the reverse direction and wrongly raised here.
        SMARTClient.verify_scope(
            "patient/*.read openid",
            ("patient/Observation.read", "openid"),
        )

    def test_granted_permission_wildcard_covers_requested(self) -> None:
        # ``patient/Observation.*`` (any permission) covers a requested
        # ``patient/Observation.read``.
        SMARTClient.verify_scope(
            "patient/Observation.*",
            ("patient/Observation.read",),
        )

    def test_different_context_does_not_satisfy(self) -> None:
        # ``user/Observation.read`` must NOT satisfy a ``patient/`` request
        # — different SMART v2 compartments.
        with pytest.raises(SMARTScopeError, match="not granted"):
            SMARTClient.verify_scope(
                "user/Observation.read",
                ("patient/Observation.read",),
            )

    def test_missing_scope_raises(self) -> None:
        with pytest.raises(SMARTScopeError, match="not granted"):
            SMARTClient.verify_scope(
                "patient/Patient.read",
                ("patient/Condition.read",),  # not granted
            )

    def test_empty_granted_raises(self) -> None:
        with pytest.raises(SMARTScopeError):
            SMARTClient.verify_scope("", ("openid",))

    def test_oidc_scopes_required_verbatim(self) -> None:
        # ``openid`` is OIDC, not a SMART resource scope — must be granted.
        with pytest.raises(SMARTScopeError, match="openid"):
            SMARTClient.verify_scope(
                "patient/Patient.read",
                ("patient/Patient.read", "openid"),
            )


# --------------------------------------------------------------------------- #
# Integration: discovery → authorize → exchange
# --------------------------------------------------------------------------- #
class TestFullLaunch:
    @pytest.mark.asyncio
    async def test_standalone_launch_end_to_end(self) -> None:
        """Walk the full launch: discover, build url, exchange code."""

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/.well-known/smart-configuration"):
                return _smart_config_response(request)
            if path.endswith("/auth/token"):
                return _token_response(request)
            return httpx.Response(404)

        transport = _build_transport(handler)
        async with _client(transport) as sc:
            d = await sc.discover()
            params = SMARTLaunchParams(
                client_id="doctoragent-app",
                redirect_uri="https://my.app/cb",
                scopes=("patient/*.read", "openid", "fhirUser", "launch/patient"),
            )
            url, verifier, state = sc.build_authorization_url(d, params=params)
            # ... EHR would redirect to redirect_uri?code=...&state=...
            result = await sc.exchange_code(
                d, code="returned-auth-code", code_verifier=verifier, state=state
            )
            SMARTClient.verify_scope(result.scope, params.scopes)

        assert result.access_token == "smart-access-token-123"
        assert result.patient == "patient-42"
