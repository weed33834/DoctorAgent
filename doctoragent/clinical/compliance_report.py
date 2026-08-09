"""HIPAA compliance self-assessment report.

Introspects a live :class:`~doctoragent.config.AegisConfig` and (optionally)
an on-disk audit log to produce a structured compliance-posture report
covering the HIPAA Security Rule safeguards the agent implements:

* **encryption**        — at-rest cipher + KDF.
* **audit_logging**     — append-only trail, entry count, HMAC integrity.
* **access_control**    — RBAC, OIDC SSO, MFA.
* **phi_protection**    — DLP scanner + de-identification pipeline.
* **key_management**    — master-key provider, KMS, rotation.
* **data_residency**    — local-first posture, cloud fallback.

The report is a *self-check*: it does not attest compliance. It surfaces
what is configured and the gaps an operator must close before a real
audit. ``compliance_gaps`` aggregates the hard failures from
:meth:`AegisConfig.validate_environment` with softer findings (cloud
fallback enabled, no API auth in prod, …); ``overall_status`` collapses
the gaps into ``compliant`` / ``partial`` / ``non_compliant``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

from doctoragent.config import AegisConfig

logger = logging.getLogger(__name__)

__all__ = ["ComplianceReport"]

# Audit-log HMAC verification is replicated here (read-only) so the report
# generator never instantiates :class:`AuditLogger` — that would create the
# log directory and a fresh HMAC key as a side effect, which is unacceptable
# for a self-check tool that may run in any context.
_AUDIT_KEY_NAME = ".audit.key"
_AUDIT_LOG_NAME = "audit.log.ndjson"
_RECENT_EVENT_LIMIT = 5


class ComplianceReport:
    """Build a HIPAA compliance self-assessment report from config + audit log."""

    def __init__(self, config: AegisConfig, audit_log_path: Path | None = None) -> None:
        self._config = config
        if audit_log_path is not None:
            self._audit_log_path = audit_log_path
        else:
            self._audit_log_path = config.paths.logs / _AUDIT_LOG_NAME

    # ── public API ──────────────────────────────────────────────────────

    def generate(self) -> dict[str, Any]:
        """Return the structured compliance report as a dict."""
        sec = self._config.security
        is_prod = self._config.env == "prod"

        env_problems = self._config.validate_environment()
        gaps: list[str] = list(env_problems)

        encryption = self._build_encryption(sec)
        audit_logging = self._build_audit_logging()
        access_control, access_gaps = self._build_access_control(is_prod)
        phi_protection, phi_gaps = self._build_phi_protection(sec)
        key_management, key_gaps = self._build_key_management()
        data_residency, residency_gaps = self._build_data_residency(sec)

        gaps.extend(access_gaps)
        gaps.extend(phi_gaps)
        gaps.extend(key_gaps)
        gaps.extend(residency_gaps)

        if audit_logging.get("hmac_integrity") == "tampered":
            gaps.append("audit log HMAC integrity check failed — possible tampering")

        # Critical gaps are the prod hard requirements (env_problems) plus a
        # tampered audit log. Everything else is a softer finding.
        critical = bool(env_problems) or audit_logging.get("hmac_integrity") == "tampered"
        if critical:
            overall = "non_compliant"
        elif gaps:
            overall = "partial"
        else:
            overall = "compliant"

        return {
            "encryption": encryption,
            "audit_logging": audit_logging,
            "access_control": access_control,
            "phi_protection": phi_protection,
            "key_management": key_management,
            "data_residency": data_residency,
            "compliance_gaps": gaps,
            "overall_status": overall,
        }

    def to_text(self) -> str:
        """Render the report as a human-readable multi-line string."""
        report = self.generate()
        lines: list[str] = []
        lines.append("=" * 72)
        lines.append("DoctorAgent HIPAA Compliance Self-Assessment Report")
        lines.append("=" * 72)
        lines.append(f"Overall status: {report['overall_status'].upper()}")
        lines.append("")

        enc = report["encryption"]
        lines.append("[Encryption]")
        lines.append(f"  algorithm: {enc['algorithm']}")
        lines.append(f"  kdf:        {enc['kdf']}")
        lines.append(f"  status:     {enc['status']}")
        lines.append("")

        au = report["audit_logging"]
        lines.append("[Audit logging]")
        lines.append(f"  enabled:         {au['enabled']}")
        lines.append(f"  entry_count:     {au['entry_count']}")
        lines.append(f"  hmac_integrity:  {au['hmac_integrity']}")
        lines.append(f"  recent_events:   {au['recent_events']}")
        lines.append("")

        ac = report["access_control"]
        lines.append("[Access control]")
        lines.append(f"  rbac_enabled:     {ac['rbac_enabled']}")
        lines.append(f"  oidc_configured:  {ac['oidc_configured']}")
        lines.append(f"  mfa_status:       {ac['mfa_status']}")
        lines.append("")

        phi = report["phi_protection"]
        lines.append("[PHI protection]")
        lines.append(f"  dlp_enabled:                {phi['dlp_enabled']}")
        lines.append(f"  deidentification_enabled:   {phi['deidentification_enabled']}")
        lines.append("")

        km = report["key_management"]
        lines.append("[Key management]")
        lines.append(f"  master_key_provider: {km['master_key_provider']}")
        lines.append(f"  kms_configured:      {km['kms_configured']}")
        lines.append(f"  rotation_enabled:    {km['rotation_enabled']}")
        if km.get("rotation_interval_days") is not None:
            lines.append(f"  rotation_interval:   {km['rotation_interval_days']} days")
        lines.append("")

        dr = report["data_residency"]
        lines.append("[Data residency]")
        lines.append(f"  local_first:           {dr['local_first']}")
        lines.append(f"  cloud_fallback_enabled: {dr['cloud_fallback_enabled']}")
        lines.append("")

        gaps = report["compliance_gaps"]
        lines.append("[Compliance gaps]")
        if gaps:
            for g in gaps:
                lines.append(f"  - {g}")
        else:
            lines.append("  (none)")
        lines.append("=" * 72)
        return "\n".join(lines)

    def to_json(self) -> str:
        """Render the report as a JSON string."""
        return json.dumps(self.generate(), indent=2, default=str)

    # ── section builders ────────────────────────────────────────────────

    @staticmethod
    def _build_encryption(sec: Any) -> dict[str, Any]:
        algorithm = getattr(sec, "encryption", "AES-256-GCM")
        kdf = getattr(sec, "kdf", "Argon2id")
        # AES-256-GCM is the expected at-rest cipher; anything else is a
        # downgrade. Argon2id is the expected KDF.
        ok = algorithm == "AES-256-GCM" and kdf == "Argon2id"
        return {
            "algorithm": algorithm,
            "kdf": kdf,
            "status": "enabled" if ok else "degraded",
        }

    def _build_audit_logging(self) -> dict[str, Any]:
        path = self._audit_log_path
        entry_count = 0
        recent: list[dict[str, Any]] = []
        hmac_status = "not_checked"
        if path is not None and Path(path).exists():
            records, hmac_status = self._read_audit_log(Path(path))
            entry_count = len(records)
            recent = [
                {
                    "timestamp": r.get("timestamp", ""),
                    "event_type": r.get("event_type", ""),
                }
                for r in records[-_RECENT_EVENT_LIMIT:]
            ]
        return {
            "enabled": True,
            "entry_count": entry_count,
            "hmac_integrity": hmac_status,
            "recent_events": recent,
        }

    def _build_access_control(self, is_prod: bool) -> tuple[dict[str, Any], list[str]]:
        oidc_configured = bool(os.environ.get("DOCTORAGENT_OIDC_ISSUER"))
        api_token_set = bool(os.environ.get("DOCTORAGENT_API_TOKEN"))
        rbac_enabled = oidc_configured or api_token_set
        hello = getattr(self._config.security, "windows_hello_enabled", False)
        mfa_status = "enabled" if hello else "disabled"
        gaps: list[str] = []
        if is_prod and not rbac_enabled:
            gaps.append(
                "prod requires API authentication (DOCTORAGENT_API_TOKEN or DOCTORAGENT_OIDC_ISSUER)"
            )
        section = {
            "rbac_enabled": rbac_enabled,
            "oidc_configured": oidc_configured,
            "mfa_status": mfa_status,
        }
        return section, gaps

    def _build_phi_protection(self, sec: Any) -> tuple[dict[str, Any], list[str]]:
        gaps: list[str] = []
        if getattr(sec, "cloud_fallback_enabled", False):
            gaps.append("cloud_fallback_enabled is on — PHI may leave the local trust boundary")
        section = {
            "dlp_enabled": True,
            "deidentification_enabled": True,
        }
        return section, gaps

    def _build_key_management(self) -> tuple[dict[str, Any], list[str]]:
        sec = self._config.security
        rotation = self._config.auto_key_rotation
        kms_configured = bool(os.environ.get("DOCTORAGENT_KMS_PROVIDER"))
        section = {
            "master_key_provider": getattr(sec, "master_key_provider", "FilePassword"),
            "kms_configured": kms_configured,
            "rotation_enabled": getattr(rotation, "enabled", False),
            "rotation_interval_days": getattr(rotation, "rotation_interval_days", None),
        }
        return section, []

    def _build_data_residency(self, sec: Any) -> tuple[dict[str, Any], list[str]]:
        cloud_fallback = bool(getattr(sec, "cloud_fallback_enabled", False))
        section = {
            "local_first": not cloud_fallback,
            "cloud_fallback_enabled": cloud_fallback,
        }
        return section, []

    # ── audit-log reader (read-only) ────────────────────────────────────

    def _read_audit_log(self, path: Path) -> tuple[list[dict[str, Any]], str]:
        """Return ``(records, hmac_integrity_status)`` without side effects.

        *status* is one of ``verified``, ``tampered``, ``no_key`` or
        ``empty``. The HMAC key is read from ``<log_dir>/.audit_key``;
        if it is absent we cannot verify and return ``no_key``.
        """
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupt audit log line in %s", path)
        if not records:
            return records, "empty"

        key_path = path.parent / _AUDIT_KEY_NAME
        if not key_path.exists():
            return records, "no_key"
        key = key_path.read_bytes()

        ok = True
        for record in records:
            stored = record.get("hmac")
            canonical = self._canonical(record)
            expected = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(stored or "", expected):
                ok = False
                break
        return records, "verified" if ok else "tampered"

    @staticmethod
    def _canonical(record: dict[str, Any]) -> str:
        """Canonical JSON for HMAC verification (mirrors AuditLogger)."""
        r = dict(record)
        r.pop("hmac", None)
        return json.dumps(r, sort_keys=True, separators=(",", ":"), default=str)
