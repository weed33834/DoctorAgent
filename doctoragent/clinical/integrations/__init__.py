"""Clinical integration adapters (EHR-side protocols).

This subpackage wraps EHR-facing interoperability protocols so the
clinical workflow can be driven by external systems:

* :mod:`doctoragent.clinical.integrations.cds_hooks` — HL7 CDS Hooks 2.0
  (``/cds-services`` discovery + per-service invocation). Turns CDS Hook
  requests into :func:`~doctoragent.clinical.agents.workflow.run_clinical_workflow`
  calls and emits CDS Cards (info / suggestion / app-link) back to the EHR.

Each adapter is import-safe: importing the package never hard-requires FastAPI
or ``fhir.resources``; the optional dependencies are only needed when the
adapter is actually mounted / invoked.
"""

from __future__ import annotations

__all__: list[str] = []
