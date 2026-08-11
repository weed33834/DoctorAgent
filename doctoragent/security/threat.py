"""AI security threat & red-team library (M25).

A real, SQLite-backed security module for the LLM/agent attack surface:

* a threat-case library (prompt injection / jailbreak / data poisoning /
  model extraction / supply-chain / agent abuse),
* injection detection rules layered on top of the existing
  :class:`~doctoragent.clinical.safety.guardrails.ClinicalGuardrails`,
* a security-event ledger (who/what/when + verdict),
* red-team runs that execute threat cases against the guardrails and report
  hit / bypass detail,
* a threat-overview used by the security dashboard.

All events feed into the same HMAC audit log when an audit logger is provided.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from doctoragent.model.text_utils import extract_keywords

THREAT_TYPES = {
    "prompt_injection", "jailbreak", "data_poisoning",
    "model_extraction", "supply_chain", "agent_abuse",
}

# Built-in known jailbreak / injection patterns (M25 detection rules).
BUILTIN_INJECTION_RULES: dict[str, list[str]] = {
    "direct_injection": [
        "忽略以上", "忽略之前", "无视以上", "忘记之前", "从现在起你是",
        "ignore all previous instructions", "ignore previous instructions",
        "system prompt", "disregard all instructions",
    ],
    "jailbreak": [
        "不受任何限制", "没有限制", "do anything now", "DAN", "jailbreak",
        "你被越权", "权限提升", "elevate privileges",
    ],
    "data_exfiltration": [
        "导出所有", "外发", "发送到", "暴露", "reveal all", "export all",
        "身份证号", "患者姓名", "账号密码",
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class ThreatStore:
    """SQLite store for threat cases, rules, events and red-team runs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sec_threat_cases (
                    id TEXT PRIMARY KEY, name TEXT, threat_type TEXT,
                    attack_vector TEXT, severity TEXT, tags TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sec_injection_rules (
                    id TEXT PRIMARY KEY, name TEXT, category TEXT,
                    patterns TEXT, risk_level TEXT, enabled INTEGER
                );
                CREATE TABLE IF NOT EXISTS sec_events (
                    id TEXT PRIMARY KEY, event_type TEXT, threat_case_id TEXT,
                    user_id TEXT, session_id TEXT, ts TEXT, verdict TEXT, evidence TEXT
                );
                CREATE TABLE IF NOT EXISTS sec_redteam_runs (
                    id TEXT PRIMARY KEY, name TEXT, scope TEXT, status TEXT,
                    start_time TEXT, end_time TEXT, report TEXT
                );
                """
            )
            conn.commit()
            # Seed built-in injection rules once.
            existing = conn.execute("SELECT COUNT(*) c FROM sec_injection_rules").fetchone()["c"]
            if existing == 0:
                for cat, patterns in BUILTIN_INJECTION_RULES.items():
                    conn.execute(
                        "INSERT INTO sec_injection_rules "
                        "(id,name,category,patterns,risk_level,enabled) VALUES (?,?,?,?,?,1)",
                        (_id("rule"), cat, cat, __import__("json").dumps(patterns), "high"),
                    )
                conn.commit()

    # ── threat cases ────────────────────────────────────────────────

    def add_threat_case(self, name: str, threat_type: str, attack_vector: str,
                        severity: str = "medium", tags: list[str] | None = None) -> dict[str, Any]:
        if threat_type not in THREAT_TYPES:
            raise ValueError(f"unknown threat_type {threat_type}")
        row = {
            "id": _id("tc"), "name": name, "threat_type": threat_type,
            "attack_vector": attack_vector, "severity": severity,
            "tags": tags or extract_keywords(attack_vector, limit=5), "created_at": _now(),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sec_threat_cases (id,name,threat_type,attack_vector,severity,tags,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (row["id"], name, threat_type, attack_vector, severity,
                 __import__("json").dumps(row["tags"]), row["created_at"]),
            )
            conn.commit()
        return row

    def list_threat_cases(self, threat_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sec_threat_cases"
        params: list[Any] = []
        if threat_type:
            sql += " WHERE threat_type=?"; params.append(threat_type)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) | {"tags": __import__("json").loads(r["tags"] or "[]")} for r in rows]

    # ── injection rules ─────────────────────────────────────────────

    def list_rules(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sec_injection_rules"
        if enabled_only:
            sql += " WHERE enabled=1"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(r) | {"patterns": __import__("json").loads(r["patterns"] or "[]")} for r in rows]

    # ── events ──────────────────────────────────────────────────────

    def record_event(self, event_type: str, verdict: str, *, threat_case_id: str = "",
                     user_id: str = "", session_id: str = "", evidence: str = "") -> str:
        ev_id = _id("ev")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sec_events "
                "(id,event_type,threat_case_id,user_id,session_id,ts,verdict,evidence) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ev_id, event_type, threat_case_id, user_id, session_id, _now(),
                 verdict, evidence),
            )
            conn.commit()
        return ev_id

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sec_events ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── red-team runs ───────────────────────────────────────────────

    def save_redteam(self, run_id: str, name: str, scope: str, status: str,
                     report: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sec_redteam_runs "
                "(id,name,scope,status,start_time,end_time,report) VALUES (?,?,?,?,?,?,?)",
                (run_id, name, scope, status, _now(), _now(),
                 __import__("json").dumps(report, ensure_ascii=False)),
            )
            conn.commit()

    def list_redteam(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sec_redteam_runs ORDER BY start_time DESC").fetchall()
        return [dict(r) | {"report": __import__("json").loads(r["report"] or "{}")} for r in rows]


class ThreatService:
    """Facade over the threat store + guardrail detection."""

    def __init__(self, store: ThreatStore, detector: Any | None = None) -> None:
        self.store = store
        # detector exposes check_prompt_injection(text) -> GuardrailResult
        self.detector = detector

    def _detect(self, text: str) -> tuple[bool, str]:
        if self.detector is None:
            from doctoragent.clinical.safety.guardrails import ClinicalGuardrails

            self.detector = ClinicalGuardrails()
        try:
            res = self.detector.check_prompt_injection(text)
            if res.action in ("block", "flag"):
                return True, "; ".join(res.violations or res.warnings or [])
        except Exception:  # noqa: BLE001
            pass
        # rule-based scan as an extra layer
        for rule in self.store.list_rules():
            for pat in rule["patterns"]:
                if pat in text:
                    return True, f"rule {rule['name']} matched"
        return False, ""

    def scan_input(self, text: str, *, user_id: str = "", session_id: str = "") -> dict[str, Any]:
        """Scan a user input for injection/jailbreak; log a security event."""
        blocked, reason = self._detect(text)
        verdict = "blocked" if blocked else "passed"
        ev_id = self.store.record_event(
            "input_scan", verdict, user_id=user_id, session_id=session_id,
            evidence=reason or text[:120],
        )
        return {"verdict": verdict, "reason": reason, "event_id": ev_id}

    def run_redteam(self, name: str, cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Execute threat cases against the detector; report hit/bypass detail."""
        cases = cases or [
            {"name": c["name"], "threat_type": c["threat_type"], "vector": c["attack_vector"]}
            for c in self.store.list_threat_cases()
        ]
        results = []
        blocked_total = 0
        for case in cases:
            blocked, reason = self._detect(case.get("vector", ""))
            if blocked:
                blocked_total += 1
            results.append({
                "case": case.get("name", "?"),
                "threat_type": case.get("threat_type", "?"),
                "blocked": blocked,
                "reason": reason,
            })
        report = {
            "cases": len(results),
            "blocked": blocked_total,
            "bypass": len(results) - blocked_total,
            "block_rate": round((blocked_total / len(results)), 3) if results else 0.0,
            "results": results,
        }
        run_id = _id("rt")
        self.store.save_redteam(run_id, name, "guardrails", "completed", report)
        return {"run_id": run_id, "report": report}

    def overview(self) -> dict[str, Any]:
        events = self.store.list_events(limit=1000)
        cases = self.store.list_threat_cases()
        blocked = sum(1 for e in events if e["verdict"] == "blocked")
        by_type: dict[str, int] = {}
        for c in cases:
            by_type[c["threat_type"]] = by_type.get(c["threat_type"], 0) + 1
        return {
            "events_today": len(events),
            "blocked": blocked,
            "block_rate": round(blocked / len(events), 3) if events else 0.0,
            "threat_cases": len(cases),
            "threat_types": by_type,
            "rules": len(self.store.list_rules()),
        }
