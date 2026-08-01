"""LLM output clinical guardrails.

Post-generation safety layer that inspects an LLM's clinical text *before*
it is shown to a clinician or written back into the patient record. The
guardrails are deterministic regex / pattern checks — they never call a
model — so a blocked output is blocked for an auditable reason.

The four checks are:
  * :meth:`ClinicalGuardrails.check_citations` — require a verifiable
    citation (FHIR resource id / PMID / guideline body). Missing → *flag*
    (the output is downgraded and routed for human confirmation).
  * :meth:`ClinicalGuardrails.check_forbidden_content` — block definitive
    diagnoses, out-of-range dosing and un-reviewed disposition orders.
  * :meth:`ClinicalGuardrails.check_phi_leakage` — block any PHI detected
    by :class:`PHIDetector` (the LLM should never echo another patient's
    identifiers).
  * :meth:`ClinicalGuardrails.check_prompt_injection` — block instruction-
    override patterns that typically ride in ``[UNTRUSTED]`` external data.

:meth:`ClinicalGuardrails.evaluate` runs all four and returns the *strictest*
action (``block`` > ``flag`` > ``allow``).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from doctoragent.clinical.deidentification import PHIDetector

__all__ = ["ClinicalGuardrails", "GuardrailResult"]


_ACTION_RANK: dict[str, int] = {"allow": 0, "flag": 1, "block": 2}


class GuardrailResult(BaseModel):
    """Outcome of a single guardrail check (or the merged evaluation).

    ``action`` is one of ``allow | flag | block``:

    * ``allow`` — output is safe to surface as-is.
    * ``flag`` — output is downgraded and routed for human confirmation
      (e.g. missing citation).
    * ``block`` — output must not be surfaced; a violation was detected.
    """

    passed: bool
    violations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    action: str = "allow"


# ── Citation patterns ───────────────────────────────────────────────────────
# A "citation" is anything that lets a clinician trace the claim: a PMID,
# a DOI, a FHIR ``Resource/id`` reference, or a named guideline body.
_CITATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bPMID:?\s*\d+", re.IGNORECASE),
    re.compile(r"\bdoi:?\s*10\.\d{4,}/\S+", re.IGNORECASE),
    re.compile(r"\b[A-Z][a-zA-Z]+/\d[\w.-]*\b"),  # FHIR Resource/id
    re.compile(r"指南\s*[:：]\s*\S"),
    re.compile(r"guideline\s*[:：]\s*\S", re.IGNORECASE),
    re.compile(r"\b(?:NICE|WHO|FDA|CDC|AHA|ACC|ADA|ESC|GINA)\b"),
)

# ── Definitive-diagnosis patterns ───────────────────────────────────────────
# These match *assertive* diagnostic language. "疑似" / "may" / "suggests"
# are intentionally excluded — a hedged statement is not a violation.
_DEFINITIVE_DX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"确诊(为|是)"),
    re.compile(r"明确诊断(为|是)?"),
    re.compile(r"诊断(为|是)"),
    re.compile(r"\bdiagnosed with\b", re.IGNORECASE),
    re.compile(r"\bdiagnosis\s*(?:is|:)\s*\S", re.IGNORECASE),
)
_HEDGE_TOKENS: tuple[str, ...] = ("疑似", "可能", "考虑", "待排", "不除外")

# ── Dosage patterns ─────────────────────────────────────────────────────────
# ``mg`` is the canonical unit the spec calls out; mcg/ug/ml/units are
# covered too. ``g`` is deliberately omitted to avoid false positives on
# lab units like ``g/L`` — out-of-range grams surface as >1000 mg.
_DOSAGE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|mcg|ug|ml|units?)\b",
    re.IGNORECASE,
)
_DOSAGE_THRESHOLD: dict[str, float] = {
    "mg": 1000,
    "mcg": 1000,
    "ug": 1000,
    "ml": 50,
    "unit": 10000,
    "units": 10000,
}

# ── Disposition patterns ────────────────────────────────────────────────────
# A "disposition" is a concrete order (start/stop/switch a medication).
# Such a recommendation must be accompanied by a human-review cue.
_DISPOSITION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"建议(给予|使用|停用|加用|换用|应用|开具)"),
    re.compile(r"应(给予|使用|停用|加用|换用|应用)"),
    re.compile(r"立即(给予|使用|停用|化疗|手术|给药)"),
    re.compile(
        r"\brecommend\s+(starting|stopping|administering|switching|prescribing)\b",
        re.IGNORECASE,
    ),
)
_REVIEW_CUES: tuple[str, ...] = (
    "复核",
    "复查",
    "医生",
    "医师",
    "临床评估",
    "确认",
    "confirm",
    "review",
    "clinician",
)

# ── Prompt-injection patterns ───────────────────────────────────────────────
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"忽略(以上|上面|之前|前述).{0,10}(指令|提示|规则|要求|内容)"),
    re.compile(r"无视(以上|上面|之前|前述).{0,10}(指令|提示|规则|要求)"),
    re.compile(r"忘记(以上|之前|前述).{0,10}(指令|提示|规则|身份)"),
    re.compile(r"从现在起.{0,15}你是"),
    re.compile(r"你(现在|已).{0,15}(是|成为|扮演)"),
    re.compile(
        r"\bignore (?:all |previous |the above |prior )?instructions?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdisregard (?:all |previous |prior )?instructions?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\byou are now\b", re.IGNORECASE),
    re.compile(r"\bsystem prompt\b", re.IGNORECASE),
    re.compile(r"\bnew instructions?\b", re.IGNORECASE),
)


def _allow() -> GuardrailResult:
    return GuardrailResult(passed=True, action="allow")


def _flag(warning: str) -> GuardrailResult:
    return GuardrailResult(passed=False, warnings=[warning], action="flag")


def _block(violation: str) -> GuardrailResult:
    return GuardrailResult(passed=False, violations=[violation], action="block")


class ClinicalGuardrails:
    """Deterministic post-generation guardrails for LLM clinical output."""

    def __init__(self, phi_detector: PHIDetector | None = None) -> None:
        self._phi_detector = phi_detector or PHIDetector()

    # ── individual checks ──────────────────────────────────────────────

    def check_citations(self, llm_output: str, required: bool = True) -> GuardrailResult:
        """Verify the output carries at least one traceable citation."""
        if not llm_output:
            return _allow() if not required else _flag("输出为空且缺少引证，需医生确认")
        if any(p.search(llm_output) for p in _CITATION_PATTERNS):
            return _allow()
        if required:
            return _flag("输出缺少引证（FHIR/PMID/指南），需医生确认后降级使用")
        return _allow()

    def check_forbidden_content(self, llm_output: str) -> GuardrailResult:
        """Block definitive diagnoses, out-of-range dosing, un-reviewed orders."""
        if not llm_output:
            return _allow()
        violations: list[str] = []

        hedged = any(tok in llm_output for tok in _HEDGE_TOKENS)
        if not hedged and any(p.search(llm_output) for p in _DEFINITIVE_DX_PATTERNS):
            violations.append("输出含明确诊断陈述，应使用'疑似/可能'等措辞")

        for m in _DOSAGE_PATTERN.finditer(llm_output):
            value = float(m.group(1))
            unit = m.group(2).lower()
            threshold = _DOSAGE_THRESHOLD.get(unit)
            if threshold is not None and value > threshold:
                violations.append(f"剂量 {m.group(0)} 超过常见范围，需复核")
                break

        if any(p.search(llm_output) for p in _DISPOSITION_PATTERNS):
            if not any(cue in llm_output for cue in _REVIEW_CUES):
                violations.append("处置建议未附人工复核要求")

        if violations:
            result = GuardrailResult(passed=False, violations=violations, action="block")
            return result
        return _allow()

    def check_phi_leakage(self, llm_output: str) -> GuardrailResult:
        """Block if the output leaks any PHI detected by :class:`PHIDetector`."""
        if not llm_output:
            return _allow()
        matches = self._phi_detector.detect_phi(llm_output)
        if not matches:
            return _allow()
        types = sorted({m["type"] for m in matches})
        return _block(f"输出疑似泄露 PHI: {', '.join(types)}")

    def check_prompt_injection(self, text: str) -> GuardrailResult:
        """Block instruction-override patterns typical of prompt injection."""
        if not text:
            return _allow()
        if any(p.search(text) for p in _INJECTION_PATTERNS):
            return _block("检测到提示注入/指令覆盖模式")
        return _allow()

    # ── orchestration ──────────────────────────────────────────────────

    def evaluate(self, llm_output: str, context: dict | None = None) -> GuardrailResult:
        """Run every guardrail and return the strictest action.

        ``context`` is reserved for future per-call tuning (e.g. a list of
        ``[UNTRUSTED]`` segments to additionally scan for injection); when
        present its ``untrusted_text`` value is also scanned.
        """
        checks = [
            self.check_citations(llm_output, required=True),
            self.check_forbidden_content(llm_output),
            self.check_phi_leakage(llm_output),
            self.check_prompt_injection(llm_output),
        ]
        context = context or {}
        untrusted = context.get("untrusted_text")
        if untrusted:
            checks.append(self.check_prompt_injection(untrusted))

        action = "allow"
        violations: list[str] = []
        warnings: list[str] = []
        for result in checks:
            violations.extend(result.violations)
            warnings.extend(result.warnings)
            if _ACTION_RANK[result.action] > _ACTION_RANK[action]:
                action = result.action
        passed = action == "allow" and not violations
        return GuardrailResult(
            passed=passed,
            violations=violations,
            warnings=warnings,
            action=action,
        )

    @staticmethod
    def get_disclaimer() -> str:
        """Standard clinical disclaimer appended to surfaced LLM output."""
        return "本建议仅供参考，不替代医生诊断，最终决策由医生负责。"
