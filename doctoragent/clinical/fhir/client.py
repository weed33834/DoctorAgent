"""Async FHIR R4 client with SMART-on-FHIR style bearer auth.

Built on :class:`httpx.AsyncClient` (already a core doctoragent dependency) and
:mod:`tenacity` for retries (mirroring ``doctoragent.model.provider`` patterns).

The client follows the **minimum-necessary** principle: ``read_patient_record``
fetches only the resource types needed for clinical summarization, with
status filters that exclude resolved / inactive problems where the FHIR search
semantics support it.

Errors are translated into a small, predictable hierarchy:

  - :class:`FHIRClientError`            — base class
  - :class:`FHIROperationError`         — server returned an OperationOutcome
                                          (4xx/5xx with a parseable OO body)
  - :class:`FHIRConnectionError`        — network / timeout / DNS failure
  - :class:`FHIRResourceNotFoundError`  — 404 on a single-resource read
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

from doctoragent.clinical.fhir.parser import (
    coding_display,
    extract_bundle_entries,
)
from doctoragent.clinical.fhir.resources import (
    SUPPORTED_READ_RESOURCES,
    parse_resource,
    serialize_resource,
)

# Detect whether the fhir.resources schema library is importable so we can
# validate inbound resources against the FHIR R4 schema. When the ``clinical``
# extra is not installed validation is a graceful no-op (the client still
# returns raw dicts, matching pre-existing behaviour on minimal installs).
try:
    import fhir.resources  # noqa: F401

    _FHIR_SCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised via the clinical-extra path
    _FHIR_SCHEMA_AVAILABLE = False

logger = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    """Retry policy: transient network errors + 5xx server errors.

    4xx responses are NOT retried (they indicate client-side problems that
    retrying won't fix — bad query, auth, missing resource, etc.).
    """
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class FHIRClientError(Exception):
    """Base class for all FHIR client errors."""


class FHIRConnectionError(FHIRClientError):
    """Network / timeout / DNS failure talking to a FHIR server."""


class FHIROperationError(FHIRClientError):
    """FHIR server returned an error (OperationOutcome or non-2xx status).

    Carries the parsed ``OperationOutcome.issue`` strings (when available) in
    ``self.issues`` and the HTTP status code in ``self.status_code``.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        issues: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.issues = issues or []


class FHIRResourceNotFoundError(FHIROperationError):
    """404 on a single-resource read."""


# --------------------------------------------------------------------------- #
# Pydantic return-shape models (downstream-friendly schema definitions)
# --------------------------------------------------------------------------- #
class PatientSummary(BaseModel):
    """Minimal patient demographics."""

    id: str = ""
    gender: str = ""
    birth_date: str = ""
    name: str = ""

    @classmethod
    def from_resource(cls, patient: dict[str, Any]) -> PatientSummary:
        name = ""
        names = patient.get("name") if isinstance(patient, dict) else None
        if isinstance(names, list) and names and isinstance(names[0], dict):
            given = names[0].get("given") or []
            family = names[0].get("family") or ""
            given_str = " ".join(given) if isinstance(given, list) else str(given)
            name = f"{given_str} {family}".strip()
        return cls(
            id=str(patient.get("id", "")) if isinstance(patient, dict) else "",
            gender=str(patient.get("gender", "")) if isinstance(patient, dict) else "",
            birth_date=str(patient.get("birthDate", "")) if isinstance(patient, dict) else "",
            name=name,
        )


class MedicationSummary(BaseModel):
    id: str = ""
    status: str = ""
    medication_text: str = ""
    authored_on: str = ""


class AllergySummary(BaseModel):
    id: str = ""
    clinical_status: str = ""
    code_text: str = ""


class LabResult(BaseModel):
    id: str = ""
    code_text: str = ""
    value: str = ""
    effective: str = ""


class EncounterSummary(BaseModel):
    id: str = ""
    status: str = ""
    class_code: str = ""
    period_start: str = ""


class PatientRecord(BaseModel):
    """Aggregated patient record returned by :meth:`FHIRClient.read_patient_record`."""

    patient: dict[str, Any] = Field(default_factory=dict)
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    encounters: list[dict[str, Any]] = Field(default_factory=list)
    medications: list[dict[str, Any]] = Field(default_factory=list)
    allergies: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class FHIRClient:
    """Async FHIR R4 REST client.

    Example::

        async with FHIRClient("https://fhir.example.com/fhir",
                              auth_token="...") as client:
            patient = await client.read("Patient", "123")
            meds = await client.read_medications("123")

    Args:
        base_url: FHIR base endpoint (e.g. ``https://fhir.example.com/fhir``).
        auth_token: Optional SMART-on-FHIR bearer token. When set, sent as
            ``Authorization: Bearer <token>``.
        timeout: Per-request timeout in seconds.
        transport: Optional :class:`httpx.BaseTransport` for testing
            (e.g. :class:`httpx.MockTransport`). Production callers leave this
            as ``None``.
    """

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        timeout: float = 30.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url or not isinstance(base_url, str):
            raise ValueError("base_url must be a non-empty string")
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        headers = {
            "Accept": "application/fhir+json",
            "Accept-Charset": "utf-8",
        }
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        client_kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "headers": headers,
            "timeout": timeout,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    # ----- lifecycle -------------------------------------------------------- #
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> FHIRClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # ----- schema validation ---------------------------------------------- #
    def _validate_resource(
        self, data: dict[str, Any], resource_type: str | None = None
    ) -> dict[str, Any]:
        """Validate *data* against the FHIR R4 schema when ``fhir.resources``
        is installed, returning *data* unchanged on success.

        Raises :class:`FHIRClientError` if the payload fails schema validation
        — malformed/foreign resources must not silently flow into downstream
        clinical reasoning. When the ``clinical`` extra is absent this is a
        no-op (the client returns raw dicts, matching legacy behaviour).
        """
        if not _FHIR_SCHEMA_AVAILABLE:
            return data
        rtype = resource_type or data.get("resourceType")
        # Only validate the resource types we know how to read; unknown types
        # pass through so the client stays usable for extension resources.
        if rtype and rtype not in SUPPORTED_READ_RESOURCES:
            return data
        try:
            parse_resource(data, resource_type)
        except ImportError:
            # fhir.resources vanished between import and call — degrade.
            return data
        except Exception as exc:  # noqa: BLE001 — surface as FHIRClientError
            raise FHIRClientError(
                f"FHIR resource failed R4 schema validation ({rtype}): {exc}"
            ) from exc
        return data

    # ----- HTTP plumbing ---------------------------------------------------- #
    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Low-level request with retry on transient network errors and 5xx.

        Network errors (``httpx.RequestError``) propagate raw so the tenacity
        predicate (see :func:`_is_retryable`) can retry them; they are wrapped
        into :class:`FHIRConnectionError` by :meth:`_request` after retries are
        exhausted. 5xx responses are raised as :class:`httpx.HTTPStatusError`
        (also retried); 4xx are returned unchanged and translated by
        :meth:`_request`.
        """
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 500:
            raise httpx.HTTPStatusError(
                f"FHIR server returned {response.status_code}",
                request=response.request,
                response=response,
            )
        return response

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send a request and return parsed JSON.

        Raises :class:`FHIRConnectionError` on network failure,
        :class:`FHIROperationError` (or 404 subclass) on non-2xx.
        """
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
            kwargs["headers"] = {"Content-Type": "application/fhir+json"}
        if params:
            # httpx accepts dict[str, str|int]; stringify defensively.
            kwargs["params"] = {k: str(v) for k, v in params.items() if v is not None}

        try:
            response = await self._send(method, path, **kwargs)
        except httpx.RequestError as exc:
            # Retries exhausted (or non-retryable); translate to a clear error.
            raise FHIRConnectionError(f"FHIR request failed: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            # 5xx after retries — translate the response into a FHIR error.
            self._raise_for_status(exc.response)
            raise  # pragma: no cover - _raise_for_status always raises

        if response.status_code >= 400:
            self._raise_for_status(response)

        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise FHIRClientError(f"FHIR server returned non-JSON body: {exc}") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Translate a non-2xx response into a :class:`FHIROperationError`."""
        status = response.status_code
        issues: list[str] = []
        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
            for issue in body.get("issue", []) or []:
                if not isinstance(issue, dict):
                    continue
                severity = issue.get("severity", "")
                details = issue.get("diagnostics") or coding_display(issue.get("details"))
                if details:
                    issues.append(f"{severity}: {details}" if severity else str(details))
        message = f"FHIR server returned HTTP {status}"
        if issues:
            message = f"{message} — {'; '.join(issues)}"
        if status == 404:
            raise FHIRResourceNotFoundError(message, status_code=status, issues=issues)
        raise FHIROperationError(message, status_code=status, issues=issues)

    # ----- public REST API -------------------------------------------------- #
    async def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        """``GET /{resource_type}/{resource_id}`` → resource dict."""
        if not resource_type or not resource_id:
            raise ValueError("resource_type and resource_id are required")
        path = f"{resource_type}/{resource_id}"
        data = await self._request("GET", path)
        if not isinstance(data, dict):
            raise FHIRClientError("Expected a FHIR resource object, got non-dict")
        # Validate against the FHIR R4 schema so a compromised/misbehaving
        # server cannot inject malformed resources into clinical reasoning.
        return self._validate_resource(data, resource_type)

    async def search(
        self,
        resource_type: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /{resource_type}?{params}`` → list of resource dicts.

        Parses a FHIR ``Bundle`` and returns just the ``entry[].resource``
        items. Returns an empty list for an empty bundle.
        """
        if not resource_type:
            raise ValueError("resource_type is required")
        data = await self._request("GET", resource_type, params=params)
        return extract_bundle_entries(data)

    async def create(self, resource_type: str, data: dict[str, Any]) -> dict[str, Any]:
        """``POST /{resource_type}`` → created resource dict.

        ``data`` should be a FHIR resource dict; ``resourceType`` is set to
        ``resource_type`` if missing.
        """
        if not resource_type:
            raise ValueError("resource_type is required")
        if not isinstance(data, dict):
            raise ValueError("data must be a dict")
        body = {**data, "resourceType": resource_type}
        # Validate the outbound body before writing so bad payloads fail fast
        # at the client rather than after a round-trip to the EHR.
        self._validate_resource(body, resource_type)
        result = await self._request("POST", resource_type, json_body=body)
        if not isinstance(result, dict):
            raise FHIRClientError("Expected a FHIR resource object, got non-dict")
        return self._validate_resource(result, resource_type)

    # ----- clinical convenience reads -------------------------------------- #
    async def read_medications(self, patient_id: str) -> list[dict[str, Any]]:
        """Active ``MedicationRequest`` resources for a patient."""
        return await self.search(
            "MedicationRequest",
            {"patient": patient_id, "status": "active"},
        )

    async def read_allergies(self, patient_id: str) -> list[dict[str, Any]]:
        """Active ``AllergyIntolerance`` resources for a patient."""
        return await self.search(
            "AllergyIntolerance",
            {"patient": patient_id, "clinical-status": "active"},
        )

    async def read_lab_results(self, patient_id: str, count: int = 20) -> list[dict[str, Any]]:
        """Recent laboratory ``Observation`` resources (newest first)."""
        return await self.search(
            "Observation",
            {
                "patient": patient_id,
                "category": "laboratory",
                "_sort": "-date",
                "_count": str(count),
            },
        )

    async def read_conditions(
        self, patient_id: str, *, active_only: bool = True
    ) -> list[dict[str, Any]]:
        """``Condition`` resources for a patient.

        With ``active_only=True`` filters to ``clinical-status=active`` (FHIR
        R4 search semantics). The Condition active-value-set also includes
        recurrences / relapses server-side; we rely on the server's
        interpretation of ``active``.
        """
        params: dict[str, Any] = {"patient": patient_id}
        if active_only:
            params["clinical-status"] = "active"
        return await self.search("Condition", params)

    async def read_encounters(self, patient_id: str, *, count: int = 5) -> list[dict[str, Any]]:
        """Most recent ``Encounter`` resources for a patient."""
        return await self.search(
            "Encounter",
            {"patient": patient_id, "_sort": "-date", "_count": str(count)},
        )

    async def read_patient_record(self, patient_id: str) -> dict[str, Any]:
        """Aggregate a minimum-necessary patient record for clinical review.

        Reads (concurrently where the server allows — emitted as parallel
        awaits here for clarity):

          - ``Patient/{id}``
          - ``Condition?patient={id}&clinical-status=active``
          - ``Encounter?patient={id}&_sort=-date&_count=5``
          - ``MedicationRequest?patient={id}&status=active``
          - ``AllergyIntolerance?patient={id}&clinical-status=active``

        Returns a :class:`PatientRecord` schema serialized to a plain dict
        (JSON-safe). Lab results are fetched separately via
        :meth:`read_lab_results` to keep this call bounded.
        """
        if not patient_id:
            raise ValueError("patient_id is required")

        patient = await self.read("Patient", patient_id)
        conditions = await self.read_conditions(patient_id, active_only=True)
        encounters = await self.read_encounters(patient_id, count=5)
        medications = await self.read_medications(patient_id)
        allergies = await self.read_allergies(patient_id)

        record = PatientRecord(
            patient=patient,
            conditions=conditions,
            encounters=encounters,
            medications=medications,
            allergies=allergies,
        )
        return record.model_dump(mode="json", exclude_none=True)


__all__ = [
    "AllergySummary",
    "EncounterSummary",
    "FHIRClient",
    "FHIRClientError",
    "FHIRConnectionError",
    "FHIROperationError",
    "FHIRResourceNotFoundError",
    "LabResult",
    "MedicationSummary",
    "PatientRecord",
    "PatientSummary",
    "SUPPORTED_READ_RESOURCES",
    "parse_resource",
    "serialize_resource",
]
