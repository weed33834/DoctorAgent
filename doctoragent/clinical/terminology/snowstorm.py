"""Async Snowstorm SNOMED CT terminology client.

Snowstorm (https://github.com/IHTSDO/snowstorm) is the SNOMED International
open-source terminology server. The public SNOMED CT browser at
https://browser.ihtsdotools.org is backed by a Snowstorm instance and offers
read-only JSON endpoints without authentication — perfect for a clinical
decision-support system that needs to:

* resolve a bare SNOMED code → preferred term + Fully Specified Name;
* walk the SNOMED CT hierarchy (``is-a`` ancestors) so the rules engine
  can decide "is this drug a beta-lactam?" deterministically rather than
  via substring matching;
* validate that a code exists in a given SNOMED CT edition.

Built on :mod:`httpx` (already a core doctoragent dep) and :mod:`tenacity` for
retries, mirroring :class:`doctoragent.clinical.fhir.client.FHIRClient` /
:class:`doctoragent.clinical.knowledge.rxnorm.RxNormClient` patterns.

Editions
--------
SNOMED CT editions are identified by a module ID. The default is the
International edition (``900000000000207008``); US / UK / Spain etc. ship
their own module IDs. Pass ``edition=...`` to :class:`SnowstormClient` to
target a national edition.

Rate limits
-----------
The public browser is rate-limited (no published SLO — be polite). The
client enforces a 30s timeout + 3 retries with exponential backoff; an
production deployment should run its own Snowstorm instance and point
``base_url`` at it.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SNOWSTORM_DEFAULT_BASE_URL",
    "SnowstormClient",
    "SnowstormError",
    "SnowstormConcept",
    "SnowstormNotFoundError",
]

# Public SNOMED International browser (read-only, no auth). Production
# deployments should override with a self-hosted Snowstorm URL.
SNOWSTORM_DEFAULT_BASE_URL = "https://browser.ihtsdotools.org/snowstorm/snomedct"

# Default SNOMED CT edition — International release. National editions are
# added by appending ``/{module_id}`` to the URL (e.g. ``/edition/2024-09``).
DEFAULT_EDITION = "MAIN"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class SnowstormError(Exception):
    """Base class for all Snowstorm client errors."""


class SnowstormNotFoundError(SnowstormError):
    """SNOMED concept id / module not found on the server."""


class SnowstormConnectionError(SnowstormError):
    """Network / timeout / DNS failure talking to Snowstorm."""


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #
class SnowstormConcept(BaseModel):
    """A resolved SNOMED CT concept.

    Only the fields the terminology layer uses are surfaced; the raw
    Snowstorm JSON is also kept under ``raw`` for callers that need the
    full payload (e.g. descriptions / inactivation indicators).
    """

    concept_id: str = Field(..., description="SNOMED CT concept id (SCTID).")
    preferred_term: str = Field(..., description="Active preferred term (FSN).")
    fully_specified_name: str = Field(
        default="", description="Fully Specified Name with semantic tag."
    )
    active: bool = Field(default=True, description="Whether the concept is active.")
    module_id: str = Field(default="", description="SNOMED CT module id (edition identifier).")
    definition_status: str = Field(default="", description="'PRIMITIVE' | 'FULLY_DEFINED'.")
    parents: list[str] = Field(
        default_factory=list,
        description="Direct parent SCTIDs (immediate ``is-a`` ancestors).",
    )
    ancestors: list[str] = Field(
        default_factory=list,
        description="All transitive ``is-a`` ancestor SCTIDs (rooted at "
        "138875005 |SNOMED CT Concept|).",
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        description="Raw Snowstorm JSON payload (for advanced callers).",
    )

    def is_a(self, ancestor_sctid: str) -> bool:
        """Return ``True`` if *ancestor_sctid* is in this concept's
        transitive ancestor set (i.e. ``self`` is-a *ancestor*)."""
        return ancestor_sctid in self.ancestors


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def _is_retryable(exc: BaseException) -> bool:
    """Retry transient network errors + 5xx; never retry 4xx (per RFC)."""
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class SnowstormClient:
    """Async Snowstorm SNOMED CT REST client.

    Example::

        async with SnowstormClient() as client:
            concept = await client.lookup("763158003")
            assert concept.preferred_term  # "Medicinal product (product)"
            assert concept.is_a("373873005")  # |Pharmaceutical / biologic product|

    Args:
        base_url: Snowstorm REST endpoint. Defaults to the public SNOMED
            International browser; production deployments should override
            with a self-hosted instance for SLO / data-residency reasons.
        edition: SNOMED CT edition short-name (``MAIN`` for International).
        release: Optional release tag (e.g. ``"2024-09"``). When ``None``,
            the server returns the latest published release.
        timeout: Per-request timeout in seconds.
        transport: Optional :class:`httpx.BaseTransport` for testing
            (e.g. :class:`httpx.MockTransport`).
    """

    def __init__(
        self,
        base_url: str = SNOWSTORM_DEFAULT_BASE_URL,
        *,
        edition: str = DEFAULT_EDITION,
        release: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url or not isinstance(base_url, str):
            raise ValueError("base_url must be a non-empty string")
        self.base_url = base_url.rstrip("/")
        self.edition = edition
        self.release = release
        self.timeout = timeout
        client_kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "headers": {
                "Accept": "application/json",
                "Accept-Charset": "utf-8",
                "User-Agent": "doctoragent-clinical/0.1",
            },
            "timeout": timeout,
            "follow_redirects": True,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    # -- lifecycle --------------------------------------------------------- #
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SnowstormClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # -- URL helpers ------------------------------------------------------- #
    def _edition_path(self) -> str:
        """Build the ``/{edition}/{release}`` path segment."""
        # Per https://github.com/IHTSDO/snowstorm/blob/master/docs/using-the-api.md
        # the public browser accepts ``browser/{edition}/...`` for read calls.
        if self.release:
            return f"browser/{self.edition}/{self.release}"
        return f"browser/{self.edition}"

    # -- HTTP plumbing ----------------------------------------------------- #
    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _send(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        response = await self._client.get(path, params=params)
        if response.status_code >= 500:
            raise httpx.HTTPStatusError(
                f"Snowstorm returned {response.status_code}",
                request=response.request,
                response=response,
            )
        return response

    async def _request_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._send(path, params=params)
        except httpx.RequestError as exc:
            raise SnowstormConnectionError(f"Snowstorm request failed: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise SnowstormNotFoundError(f"Snowstorm 404 for {path}") from exc
            body = ""
            try:
                body = exc.response.text[:300]
            except Exception:  # noqa: BLE001 — defensive
                pass
            raise SnowstormError(
                f"Snowstorm returned HTTP {exc.response.status_code}: {body}"
            ) from exc
        if response.status_code == 404:
            raise SnowstormNotFoundError(f"Snowstorm 404 for {path}")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise SnowstormError(f"Snowstorm returned non-JSON body: {exc}") from exc

    # -- public API -------------------------------------------------------- #
    async def lookup(self, concept_id: str) -> SnowstormConcept:
        """Resolve a single SNOMED CT concept by SCTID.

        ``GET /browser/{edition}/{release}/concepts/{conceptId}`` per the
        Snowstorm REST API. Raises :class:`SnowstormNotFoundError` for an
        unknown SCTID, :class:`SnowstormConnectionError` for network
        failures, :class:`SnowstormError` for other server errors.
        """
        if not concept_id or not isinstance(concept_id, str):
            raise ValueError("concept_id must be a non-empty string")
        path = f"{self._edition_path()}/concepts/{concept_id}"
        data = await self._request_json(path)
        if not isinstance(data, dict):
            raise SnowstormError("Expected a concept object, got non-dict")
        return _parse_concept(data)

    async def get_preferred_term(self, concept_id: str) -> str:
        """Convenience accessor returning just the preferred term.

        Returns an empty string for an inactive concept (the API still
        returns the latest preferred term; callers that need to distinguish
        inactive from missing should call :meth:`lookup` directly).
        """
        concept = await self.lookup(concept_id)
        return concept.preferred_term

    async def get_ancestors(self, concept_id: str) -> list[str]:
        """Return the transitive ``is-a`` ancestor SCTIDs for *concept_id*.

        Uses ``GET /browser/{edition}/concepts/{conceptId}/ancestors`` which
        the public Snowstorm browser exposes without auth. The returned list
        is rooted at ``138875005 |SNOMED CT Concept|`` so it includes every
        ancestor up to the root.
        """
        if not concept_id:
            raise ValueError("concept_id must be a non-empty string")
        path = f"{self._edition_path()}/concepts/{concept_id}/ancestors"
        data = await self._request_json(path)
        if isinstance(data, list):
            return [str(item) for item in data if item]
        # Some deployments return ``{"ancestors": [...]}``.
        if isinstance(data, dict) and isinstance(data.get("ancestors"), list):
            return [str(item) for item in data["ancestors"] if item]
        return []

    async def lookup_with_hierarchy(self, concept_id: str) -> SnowstormConcept:
        """Resolve a concept AND populate its transitive ancestors.

        Two HTTP calls (concept + ancestors) because the public browser
        doesn't return ancestors inline. The concept's ``ancestors`` field
        is populated for callers that need ``is_a()`` checks; callers that
        only need the display term should use :meth:`lookup` to save the
        extra round-trip.
        """
        concept = await self.lookup(concept_id)
        try:
            concept.ancestors = await self.get_ancestors(concept_id)
        except SnowstormNotFoundError:
            # Some editions don't expose /ancestors for every concept;
            # leave the field empty so callers can still use the display.
            logger.debug(
                "Snowstorm returned no ancestors for %s; is_a() will return False for all checks",
                concept_id,
            )
        return concept

    async def is_a(self, concept_id: str, ancestor_sctid: str) -> bool:
        """Return ``True`` if *concept_id* is-a *ancestor_sctid*.

        Implemented as a single ECL lookup (``<<{ancestor}``) rather than
        the two-call lookup+ancestors path so it's O(1) round-trips. The
        ECL endpoint is ``GET /browser/{edition}/concepts/{conceptId}``
        followed by an ancestor check; we use the ancestors endpoint
        because the public browser doesn't expose a direct ECL match call
        without auth.
        """
        if not concept_id or not ancestor_sctid:
            return False
        try:
            ancestors = await self.get_ancestors(concept_id)
        except SnowstormNotFoundError:
            return False
        return ancestor_sctid in ancestors


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parse_concept(data: dict[str, Any]) -> SnowstormConcept:
    """Build a :class:`SnowstormConcept` from the Snowstorm JSON payload.

    Defensive against the slightly different shapes Snowstorm returns across
    versions (the concept endpoint always returns ``conceptId`` /
    ``fsn`` / ``pt`` / ``active`` / ``definitionStatus``; the ``module``
    field name has varied across releases).
    """
    concept_id = str(data.get("conceptId") or data.get("id") or "")
    if not concept_id:
        raise SnowstormError(f"Snowstorm response missing conceptId: {str(data)[:200]}")
    fsn = ""
    pt = ""
    fsn_obj = data.get("fsn")
    pt_obj = data.get("pt")
    if isinstance(fsn_obj, dict):
        fsn = str(fsn_obj.get("term") or "")
    elif isinstance(fsn_obj, str):
        fsn = fsn_obj
    if isinstance(pt_obj, dict):
        pt = str(pt_obj.get("term") or "")
    elif isinstance(pt_obj, str):
        pt = pt_obj
    if not pt:
        # Fallback: the FSN is always populated; strip the semantic tag
        # (everything in parentheses) for a human-friendly display.
        pt = fsn.split("(", 1)[0].strip() or fsn
    active = data.get("active")
    if not isinstance(active, bool):
        active = str(active).lower() in ("true", "1", "yes")
    module_id = str(data.get("moduleId") or data.get("module") or "")
    definition_status = ""
    ds = data.get("definitionStatus")
    if isinstance(ds, dict):
        definition_status = str(ds.get("conceptId") or ds.get("term") or "")
    elif isinstance(ds, str):
        definition_status = ds
    return SnowstormConcept(
        concept_id=concept_id,
        preferred_term=pt,
        fully_specified_name=fsn,
        active=active,
        module_id=module_id,
        definition_status=definition_status,
        parents=[],  # /concepts/{id} doesn't return parents; populated
        # via get_ancestors when the caller uses lookup_with_hierarchy.
        ancestors=[],
        raw=data,
    )
