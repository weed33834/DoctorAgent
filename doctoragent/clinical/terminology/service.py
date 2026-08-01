"""TerminologyService — single façade for clinical code lookup.

Resolves a ``(system_uri, code)`` pair (e.g.
``("http://loinc.org", "8867-4")`` → ``"Heart rate"``) to a human-readable
display + optional metadata. The service unifies the four clinical code
systems DoctorAgent supports:

* **SNOMED CT** — looked up live via :class:`SnowstormClient` (preferred term
  + hierarchy, for ``is_a()`` queries the rules engine needs).
* **LOINC** — curated local map (:mod:`loinc_map`); enterprise deployments
  can append the full NLM table via :func:`load_loinc_table`.
* **ICD-10-CM** — curated local map (:mod:`icd10_map`); CDC table can be
  appended via :func:`load_icd10_table`.
* **RxNorm** — delegated to the existing
  :class:`doctoragent.clinical.knowledge.rxnorm.RxNormClient` (no
  re-implementation; drug-name normalisation already lives there).

Design goals
------------
1. **One call site** — every layer (CDS Hooks translator, FHIR parser,
   rules engine, LLM prompts) resolves codes through
   :meth:`TerminologyService.lookup_display` instead of carrying its own
   ``{"8867-4": "Heart rate"}`` literals.
2. **Sync-by-default, async-on-demand** — the curated maps are pure dict
   lookups, so the common path is sync. SNOMED CT hierarchy lookups are
   async because they hit the network; callers that need them await
   :meth:`lookup_snomed_concept` directly.
3. **Graceful degradation** — when the Snowstorm client is unreachable or
   not configured, the service still returns the LOINC/ICD-10-CM display
   from the local maps. SNOMED ``is_a()`` simply returns ``False``.
4. **Never raises on unknown codes** — the contract is "best-effort
   resolution". Unknown systems/codes return ``None``; callers decide
   whether to surface ``Coding.display`` instead. The exception is
   :meth:`lookup_display_strict` which raises on unreachable Snowstorm
   servers (for tests that need to assert the network path).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from doctoragent.clinical.terminology.codesystems import (
    CodeSystem,
    parse_system_uri,
)
from doctoragent.clinical.terminology.icd10_map import (
    is_valid_icd10_cm_format,
    lookup_icd10_display,
)
from doctoragent.clinical.terminology.loinc_map import (
    lookup_loinc_display,
    lookup_loinc_test_name,
)
from doctoragent.clinical.terminology.snowstorm import (
    SnowstormClient,
    SnowstormError,
    SnowstormNotFoundError,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from doctoragent.clinical.knowledge.rxnorm import RxNormClient

logger = logging.getLogger(__name__)

__all__ = [
    "TerminologyService",
    "TerminologyServiceError",
    "TerminologyServiceResult",
    "lookup_display",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class TerminologyServiceError(Exception):
    """Base class for TerminologyService failures (Snowstorm only)."""


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #
class TerminologyServiceResult:
    """Outcome of a terminology lookup.

    Carries both the resolved display and the source it came from so
    callers can render provenance ("SNOMED CT preferred term" vs "local
    curated map") in audit logs / LLM prompts without re-querying.
    """

    __slots__ = ("code", "code_system", "display", "source", "extra")

    def __init__(
        self,
        *,
        code: str,
        code_system: CodeSystem,
        display: str | None,
        source: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.code_system = code_system
        self.display = display
        self.source = source
        self.extra = extra or {}

    @property
    def found(self) -> bool:
        """``True`` when a non-empty display was resolved."""
        return bool(self.display)

    def __repr__(self) -> str:
        return (
            f"TerminologyServiceResult(code={self.code!r}, "
            f"system={self.code_system.value!r}, display={self.display!r}, "
            f"source={self.source!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TerminologyServiceResult):
            return NotImplemented
        return (
            self.code == other.code
            and self.code_system == other.code_system
            and self.display == other.display
            and self.source == other.source
        )


# Source tags reported in :attr:`TerminologyServiceResult.source`. Stable
# strings so audit-log assertions / dashboards can group by source.
SOURCE_CURATED_LOINC = "loinc_curated"
SOURCE_CURATED_ICD10 = "icd10_curated"
SOURCE_SNOWSTORM = "snomed_snowstorm"
SOURCE_RXNORM = "rxnorm_api"
SOURCE_FALLBACK_DISPLAY = "resource_display"
SOURCE_UNKNOWN = "unknown"

# Sources that resolved the display from an authoritative terminology
# provider (curated map / live API), as opposed to merely echoing the
# resource's own ``Coding.display``. Used by
# :meth:`TerminologyService.resolve_codeable_concept` to prefer a
# real resolution over a fallback display when walking a multi-coding
# CodeableConcept.
_AUTHORITATIVE_SOURCES: frozenset[str] = frozenset(
    {
        SOURCE_CURATED_LOINC,
        SOURCE_CURATED_ICD10,
        SOURCE_SNOWSTORM,
        SOURCE_RXNORM,
    }
)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class TerminologyService:
    """Façade resolving ``Coding`` → display across all supported code systems.

    Parameters
    ----------
    snowstorm_client:
        Optional :class:`SnowstormClient` for SNOMED CT lookups. When
        ``None`` (default), SNOMED-coded concepts fall back to the
        resource's own ``display`` (graceful degradation — no network
        call, no error). Inject a client in deployments that need
        hierarchy / ``is_a()`` checks.
    rxnorm_client:
        Optional :class:`RxNormClient` for RxNorm-coded concepts. When
        ``None``, RxNorm lookups return ``None`` (the curated map layer
        doesn't cover RxNorm; it's all live-API).
    load_tables:
        When ``True`` (default), eagerly call :func:`load_loinc_table`
        and :func:`load_icd10_table` to pick up any env-configured bulk
        tables. Set to ``False`` in unit tests / cold-start-sensitive
        paths to avoid touching the filesystem.

    Lifecycle
    ---------
    The service holds a reference to the (optional) Snowstorm client, so
    its lifecycle is tied to it. When you constructed the service with a
    Snowstorm client, call :meth:`aclose` at shutdown (or use ``async
    with``) to release the underlying httpx connection pool::

        async with SnowstormClient() as sc:
            svc = TerminologyService(snowstorm_client=sc)
            concept = await svc.lookup_snomed_concept("763158003")
    """

    def __init__(
        self,
        *,
        snowstorm_client: SnowstormClient | None = None,
        rxnorm_client: RxNormClient | None = None,
        load_tables: bool = True,
    ) -> None:
        self.snowstorm_client = snowstorm_client
        self.rxnorm_client = rxnorm_client
        if load_tables:
            # Eagerly pick up env-configured bulk tables. No-op when the
            # env vars are unset — safe to call on every construction.
            from doctoragent.clinical.terminology.icd10_map import load_icd10_table
            from doctoragent.clinical.terminology.loinc_map import load_loinc_table

            try:
                load_loinc_table()
            except Exception:  # noqa: BLE001 — defensive; tables are optional
                logger.warning("Failed to load LOINC table", exc_info=True)
            try:
                load_icd10_table()
            except Exception:  # noqa: BLE001 — defensive
                logger.warning("Failed to load ICD-10-CM table", exc_info=True)

    # -- lifecycle --------------------------------------------------------- #
    async def aclose(self) -> None:
        """Close the Snowstorm client if we own one."""
        if self.snowstorm_client is not None:
            await self.snowstorm_client.aclose()

    async def __aenter__(self) -> TerminologyService:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # -- public API: sync lookup ------------------------------------------- #
    def lookup_display(
        self,
        system_uri: str,
        code: str,
        *,
        fallback_display: str | None = None,
    ) -> TerminologyServiceResult:
        """Synchronously resolve ``(system_uri, code)`` to a display.

        Hits the curated LOINC / ICD-10-CM maps; SNOMED CT returns the
        ``fallback_display`` (the resource's own display) because a live
        Snowstorm call is async — callers needing SNOMED preferred terms
        must use :meth:`lookup_snomed_concept` / :meth:`lookup_display_async`.

        Parameters
        ----------
        system_uri:
            FHIR ``Coding.system`` URI (e.g. ``"http://loinc.org"``).
            Tolerantly parsed — variant spellings are accepted.
        code:
            The ``Coding.code`` value (e.g. ``"8867-4"``).
        fallback_display:
            The resource's own ``Coding.display`` (when present). Used as
            the result display when no other source resolves a display,
            so the caller always gets *something* to render.

        Returns
        -------
        :class:`TerminologyServiceResult` — never raises. ``display`` is
        ``None`` only when both the curated map and ``fallback_display``
        are empty.
        """
        if not isinstance(code, str) or not code:
            return TerminologyServiceResult(
                code="",
                code_system=CodeSystem.UNKNOWN,
                display=fallback_display,
                source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
            )
        code = code.strip()
        system = parse_system_uri(system_uri)

        if system == CodeSystem.LOINC:
            display = lookup_loinc_display(code)
            if display:
                return TerminologyServiceResult(
                    code=code,
                    code_system=system,
                    display=display,
                    source=SOURCE_CURATED_LOINC,
                )
        elif system == CodeSystem.ICD10_CM:
            display = lookup_icd10_display(code)
            if display:
                return TerminologyServiceResult(
                    code=code,
                    code_system=system,
                    display=display,
                    source=SOURCE_CURATED_ICD10,
                )
            # Structural validity is useful provenance even when the code
            # isn't in the curated map (a "valid-but-unknown" code is
            # probably a real CDC code we just don't ship).
            if is_valid_icd10_cm_format(code):
                return TerminologyServiceResult(
                    code=code,
                    code_system=system,
                    display=fallback_display,
                    source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
                    extra={"format_valid": True, "in_curated_map": False},
                )
        elif system == CodeSystem.SNOMED_CT:
            # Sync path can't hit Snowstorm; fall back to the resource's
            # own display. Callers needing the preferred term must await
            # lookup_display_async / lookup_snomed_concept.
            return TerminologyServiceResult(
                code=code,
                code_system=system,
                display=fallback_display,
                source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
                extra={"requires_async_lookup": True},
            )
        elif system == CodeSystem.RXNORM:
            # RxNorm is API-only; sync path can't resolve it.
            return TerminologyServiceResult(
                code=code,
                code_system=system,
                display=fallback_display,
                source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
                extra={"requires_async_lookup": True},
            )

        # Unknown system or known system but no curated entry.
        return TerminologyServiceResult(
            code=code,
            code_system=system,
            display=fallback_display,
            source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
        )

    # -- public API: async lookup ------------------------------------------ #
    async def lookup_display_async(
        self,
        system_uri: str,
        code: str,
        *,
        fallback_display: str | None = None,
    ) -> TerminologyServiceResult:
        """Async variant of :meth:`lookup_display` — resolves SNOMED + RxNorm.

        For LOINC / ICD-10-CM, this is identical to the sync path (the
        curated map lookup is a dict access — no I/O). For SNOMED CT it
        issues a live Snowstorm call (when a client is configured) and
        returns the preferred term. For RxNorm it delegates to the
        RxNorm REST client to fetch the canonical drug name.

        On any failure (network, 404, no client configured), the method
        falls back to the local curated map (if any) and then to
        ``fallback_display`` — never raises.
        """
        if not isinstance(code, str) or not code:
            return TerminologyServiceResult(
                code="",
                code_system=CodeSystem.UNKNOWN,
                display=fallback_display,
                source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
            )
        code = code.strip()
        system = parse_system_uri(system_uri)

        # LOINC / ICD-10-CM — pure local, same as sync path.
        if system in (CodeSystem.LOINC, CodeSystem.ICD10_CM):
            return self.lookup_display(system_uri, code, fallback_display=fallback_display)

        # SNOMED CT — live Snowstorm call when configured.
        if system == CodeSystem.SNOMED_CT:
            return await self._lookup_snomed_display(code, fallback_display=fallback_display)

        # RxNorm — live RxNorm call when configured.
        if system == CodeSystem.RXNORM:
            return await self._lookup_rxnorm_display(code, fallback_display=fallback_display)

        return TerminologyServiceResult(
            code=code,
            code_system=system,
            display=fallback_display,
            source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
        )

    async def lookup_snomed_concept(self, concept_id: str) -> Any:
        """Resolve a SNOMED CT concept via Snowstorm.

        Returns the :class:`SnowstormConcept` (with ancestors populated
        when available). Raises :class:`TerminologyServiceError` when no
        Snowstorm client is configured, and propagates
        :class:`SnowstormNotFoundError` / :class:`SnowstormError` from
        the underlying client.
        """
        if self.snowstorm_client is None:
            raise TerminologyServiceError(
                "SNOMED CT lookup requires a SnowstormClient — "
                "pass one to TerminologyService(snowstorm_client=...)"
            )
        try:
            return await self.snowstorm_client.lookup_with_hierarchy(concept_id)
        except SnowstormNotFoundError:
            raise
        except SnowstormError as exc:
            raise TerminologyServiceError(
                f"Snowstorm lookup failed for {concept_id}: {exc}"
            ) from exc

    async def is_a(self, concept_id: str, ancestor_sctid: str) -> bool:
        """``True`` if *concept_id* is-a *ancestor_sctid* in SNOMED CT.

        Returns ``False`` (never raises) when:
        * no Snowstorm client is configured (graceful degradation),
        * the concept is unknown (404),
        * any transient error occurs (logged at WARNING).

        Used by the rules engine to decide "is this drug a beta-lactam?"
        deterministically rather than by substring match.
        """
        if self.snowstorm_client is None:
            return False
        try:
            return await self.snowstorm_client.is_a(concept_id, ancestor_sctid)
        except SnowstormNotFoundError:
            return False
        except SnowstormError:
            logger.warning(
                "Snowstorm is_a(%s, %s) failed; returning False",
                concept_id,
                ancestor_sctid,
                exc_info=True,
            )
            return False

    # -- convenience: FHIR Coding → display -------------------------------- #
    def resolve_coding(
        self,
        coding: dict[str, Any],
    ) -> TerminologyServiceResult:
        """Resolve a bare FHIR ``Coding`` dict (``system`` + ``code`` + ``display``).

        Convenience wrapper around :meth:`lookup_display` that pulls the
        three fields out of a Coding dict, so callers don't have to::

            result = svc.resolve_coding(obs["code"]["coding"][0])
            print(result.display)
        """
        if not isinstance(coding, dict):
            return TerminologyServiceResult(
                code="",
                code_system=CodeSystem.UNKNOWN,
                display=None,
                source=SOURCE_UNKNOWN,
            )
        system = coding.get("system")
        code = coding.get("code")
        fallback = coding.get("display")
        if isinstance(fallback, str):
            fallback = fallback.strip() or None
        else:
            fallback = None
        return self.lookup_display(
            system if isinstance(system, str) else "",
            code if isinstance(code, str) else "",
            fallback_display=fallback,
        )

    def resolve_codeable_concept(
        self,
        codeable: dict[str, Any],
    ) -> TerminologyServiceResult:
        """Resolve the first coding of a FHIR ``CodeableConcept``.

        Walks the ``coding`` list and returns the first result that
        resolves via an **authoritative** source (curated LOINC / ICD-10-CM
        map, Snowstorm, RxNorm API). When no coding resolves
        authoritatively, the first coding's ``display`` fallback is
        returned (so the caller still gets the resource's own label).
        Prefer this over :meth:`resolve_coding` when the input is a full
        CodeableConcept (the common FHIR shape).
        """
        if not isinstance(codeable, dict):
            return TerminologyServiceResult(
                code="",
                code_system=CodeSystem.UNKNOWN,
                display=None,
                source=SOURCE_UNKNOWN,
            )
        # The resource's own ``text`` wins — it's the human-curated label
        # the EHR chose to surface.
        text = codeable.get("text")
        if isinstance(text, str) and text.strip():
            return TerminologyServiceResult(
                code="",
                code_system=CodeSystem.UNKNOWN,
                display=text.strip(),
                source=SOURCE_FALLBACK_DISPLAY,
            )
        codings = codeable.get("coding")
        if not isinstance(codings, list) or not codings:
            return TerminologyServiceResult(
                code="",
                code_system=CodeSystem.UNKNOWN,
                display=None,
                source=SOURCE_UNKNOWN,
            )
        # Two passes: prefer an authoritative resolution, then fall back
        # to the first coding that at least carried a display string.
        first_fallback: TerminologyServiceResult | None = None
        for coding in codings:
            if not isinstance(coding, dict):
                continue
            result = self.resolve_coding(coding)
            if result.source in _AUTHORITATIVE_SOURCES and result.display:
                return result
            if first_fallback is None and result.display:
                first_fallback = result
        return first_fallback or TerminologyServiceResult(
            code="",
            code_system=CodeSystem.UNKNOWN,
            display=None,
            source=SOURCE_UNKNOWN,
        )

    def loinc_to_test_name(self, loinc_code: str) -> str | None:
        """Map a LOINC code to the rule-engine test name (e.g. ``"heart_rate"``).

        Thin wrapper over :func:`lookup_loinc_test_name`; kept on the
        service so the CDS Hooks translator depends only on
        :class:`TerminologyService` rather than reaching into the
        ``loinc_map`` module directly.
        """
        return lookup_loinc_test_name(loinc_code)

    # -- internals ---------------------------------------------------------- #
    async def _lookup_snomed_display(
        self,
        code: str,
        *,
        fallback_display: str | None,
    ) -> TerminologyServiceResult:
        if self.snowstorm_client is None:
            return TerminologyServiceResult(
                code=code,
                code_system=CodeSystem.SNOMED_CT,
                display=fallback_display,
                source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
                extra={"requires_snowstorm_client": True},
            )
        try:
            concept = await self.snowstorm_client.lookup(code)
        except SnowstormNotFoundError:
            return TerminologyServiceResult(
                code=code,
                code_system=CodeSystem.SNOMED_CT,
                display=fallback_display,
                source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
                extra={"not_found": True},
            )
        except SnowstormError:
            logger.warning(
                "Snowstorm lookup failed for SNOMED %s; falling back",
                code,
                exc_info=True,
            )
            return TerminologyServiceResult(
                code=code,
                code_system=CodeSystem.SNOMED_CT,
                display=fallback_display,
                source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
                extra={"snowstorm_error": True},
            )
        return TerminologyServiceResult(
            code=code,
            code_system=CodeSystem.SNOMED_CT,
            display=concept.preferred_term or fallback_display,
            source=SOURCE_SNOWSTORM,
            extra={
                "fully_specified_name": concept.fully_specified_name,
                "active": concept.active,
                "module_id": concept.module_id,
            },
        )

    async def _lookup_rxnorm_display(
        self,
        code: str,
        *,
        fallback_display: str | None,
    ) -> TerminologyServiceResult:
        if self.rxnorm_client is None:
            return TerminologyServiceResult(
                code=code,
                code_system=CodeSystem.RXNORM,
                display=fallback_display,
                source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
                extra={"requires_rxnorm_client": True},
            )
        try:
            info = await self.rxnorm_client.get_drug_info(code)
        except Exception:  # noqa: BLE001 — RxNorm errors are non-fatal
            logger.warning("RxNorm lookup failed for RxCUI %s; falling back", code, exc_info=True)
            return TerminologyServiceResult(
                code=code,
                code_system=CodeSystem.RXNORM,
                display=fallback_display,
                source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
                extra={"rxnorm_error": True},
            )
        name = info.get("name") if isinstance(info, dict) else None
        if not name:
            return TerminologyServiceResult(
                code=code,
                code_system=CodeSystem.RXNORM,
                display=fallback_display,
                source=SOURCE_FALLBACK_DISPLAY if fallback_display else SOURCE_UNKNOWN,
                extra={"not_found": True},
            )
        return TerminologyServiceResult(
            code=code,
            code_system=CodeSystem.RXNORM,
            display=name,
            source=SOURCE_RXNORM,
            extra={
                "tty": info.get("tty"),
                "brand_names": info.get("brand_names", []),
            },
        )


# --------------------------------------------------------------------------- #
# Module-level singleton + free function (ergonomic default path)
# --------------------------------------------------------------------------- #
_default_service: TerminologyService | None = None


def get_default_service() -> TerminologyService:
    """Return the process-wide default :class:`TerminologyService`.

    Lazily constructed on first call so import-time never spins up a
    Snowstorm client. The default service has NO Snowstorm / RxNorm
    clients (sync-only) — call :func:`configure_default_service` once at
    startup to inject them when hierarchy / drug-name normalisation is
    needed.
    """
    global _default_service
    if _default_service is None:
        _default_service = TerminologyService(load_tables=True)
    return _default_service


def configure_default_service(service: TerminologyService) -> None:
    """Override the module-level default service.

    Called once at application startup (after constructing the Snowstorm /
    RxNorm clients) so the CDS Hooks translator and FHIR parser can reach
    a fully-configured service via :func:`get_default_service` without
    threading it through every call site.
    """
    global _default_service
    _default_service = service


def lookup_display(
    system_uri: str,
    code: str,
    *,
    fallback_display: str | None = None,
) -> str | None:
    """Free-function shortcut over the default service.

    Resolves ``(system_uri, code)`` → display string (or ``None``) via
    the module-level default service. Kept for callers that don't want
    to hold a :class:`TerminologyService` reference (e.g. the FHIR
    parser, which is imported long before the app boots).
    """
    result = get_default_service().lookup_display(
        system_uri, code, fallback_display=fallback_display
    )
    return result.display
