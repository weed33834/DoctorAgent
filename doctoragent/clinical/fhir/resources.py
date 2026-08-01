"""FHIR R4 resource parsing, serialization and validation helpers.

Thin wrapper around the HL7-official ``fhir.resources`` pydantic library
(``pip install fhir.resources``). All functions are defensive: malformed input
raises :class:`ValueError` / :class:`ImportError` with a clear message rather
than silently returning a half-built object.

The functions here operate on plain JSON-ish ``dict`` objects (the wire format
used by FHIR servers) and convert to/from typed pydantic model instances
exposed by ``fhir.resources``.
"""

from __future__ import annotations

import json
from typing import Any

try:
    from fhir.resources import get_fhir_model_class
    from pydantic import ValidationError

    _FHIR_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via graceful import path
    _FHIR_AVAILABLE = False
    get_fhir_model_class = None  # type: ignore[assignment]
    ValidationError = None  # type: ignore[assignment]


# Resource types that the clinical adapter knows how to *read* (write/create is
# more permissive). Kept as a plain list (not enum) so callers can do simple
# ``in`` checks without importing anything extra.
SUPPORTED_READ_RESOURCES: list[str] = [
    "Patient",
    "Condition",
    "Encounter",
    "MedicationRequest",
    "MedicationDispense",
    "AllergyIntolerance",
    "Observation",
    "DocumentReference",
    "ClinicalImpression",
]


def _require_fhir() -> None:
    """Raise a clear ImportError if ``fhir.resources`` is not installed."""
    if not _FHIR_AVAILABLE:
        raise ImportError(
            "fhir.resources is required for FHIR R4 support. "
            "Install it with: pip install fhir.resources"
        )


def _coerce_dict(data: dict[str, Any] | str) -> dict[str, Any]:
    """Accept either a dict or a JSON string and return a dict."""
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecode as exc:
            raise ValueError(f"FHIR data is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("FHIR data must decode to a JSON object")
        return parsed
    if not isinstance(data, dict):
        raise TypeError(f"FHIR data must be a dict or JSON string, got {type(data).__name__}")
    return data


def _resolve_resource_type(data: dict[str, Any], resource_type: str | None) -> str:
    """Determine the FHIR resource type to use for parsing."""
    if resource_type:
        return resource_type
    rt = data.get("resourceType")
    if not rt:
        raise ValueError(
            "resource_type must be provided or data must contain a 'resourceType' field"
        )
    if not isinstance(rt, str):
        raise ValueError(f"'resourceType' must be a string, got {type(rt).__name__}")
    return rt


def parse_resource(data: dict[str, Any] | str, resource_type: str | None = None) -> Any:
    """Parse a FHIR resource (dict or JSON string) into a typed model instance.

    Args:
        data: FHIR resource as ``dict`` or JSON ``str``.
        resource_type: Optional resource type override (e.g. ``"Patient"``).
            If omitted, inferred from ``data["resourceType"]``.

    Returns:
        A ``fhir.resources`` pydantic model instance (e.g. ``Patient``).

    Raises:
        ImportError: if ``fhir.resources`` is not installed.
        ValueError: if data is malformed JSON or missing resourceType.
        pydantic.ValidationError: if the payload violates the FHIR R4 schema.
    """
    _require_fhir()
    payload = _coerce_dict(data)
    rtype = _resolve_resource_type(payload, resource_type)
    try:
        model_class = get_fhir_model_class(rtype)
    except KeyError as exc:
        raise ValueError(f"Unknown FHIR resource type: {rtype!r}") from exc
    # Ensure resourceType on payload matches what we resolved (fhir.resources
    # validates this field strictly for some resources).
    payload = {**payload, "resourceType": rtype}
    return model_class.model_validate(payload)


def serialize_resource(resource: Any) -> dict[str, Any]:
    """Serialize a FHIR model instance to a JSON-safe dict (None fields dropped).

    Args:
        resource: A ``fhir.resources`` model instance.

    Returns:
        JSON-serializable dict (ready for ``json.dumps`` or HTTP body).
    """
    _require_fhir()
    if resource is None:
        raise ValueError("resource must not be None")
    # ``model_dump`` with mode="json" coerces datetimes/decimals to JSON-safe
    # primitives; exclude_none keeps the output compact (FHIR convention).
    return resource.model_dump(mode="json", exclude_none=True)


def validate_resource(resource: Any) -> list[str]:
    """Validate an in-memory FHIR model instance.

    Args:
        resource: A ``fhir.resources`` model instance (already parsed).

    Returns:
        List of human-readable validation error strings. Empty list means the
        resource is valid.
    """
    _require_fhir()
    if resource is None:
        return ["resource must not be None"]
    # Re-validate by serializing + re-parsing: catches in-place mutations that
    # would have escaped the original parse-time validation.
    try:
        payload = serialize_resource(resource)
        rtype = payload.get("resourceType")
        if not rtype:
            return ["missing 'resourceType' field"]
        model_class = get_fhir_model_class(rtype)
        model_class.model_validate(payload)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(p) for p in err.get('loc', ()) or ())}: {err.get('msg', '')}"
            for err in exc.errors()
        ]
    except ValueError as exc:
        return [str(exc)]
    return []


__all__ = [
    "SUPPORTED_READ_RESOURCES",
    "parse_resource",
    "serialize_resource",
    "validate_resource",
]
