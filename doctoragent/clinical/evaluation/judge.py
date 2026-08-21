"""LLM-as-judge for free-text clinical rationale evaluation.

The judge scores whether a model's free-text answer is clinically correct
and well-supported. To avoid self-preference bias, the judge **must** belong
to a different model family than the model-under-test (MUT) — see
:func:`detect_model_family` and the ``cross_family`` enforcement in
:class:`LLMJudge`.

When no LLM is available the judge degrades to a deterministic
semantic-overlap scorer (token Jaccard against the gold rationale) so the
benchmark remains runnable offline.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

__all__ = [
    "JUDGE_FAMILY",
    "JudgeFamily",
    "JudgeVerdict",
    "LLMJudge",
    "detect_model_family",
]


# ---------------------------------------------------------------------------
# Model family detection (cross-family judging)
# ---------------------------------------------------------------------------


class JudgeFamily(str, Enum):
    """Coarse model family buckets used to enforce cross-family judging.

    A judge and model-under-test in the same family raise a
    :class:`ValueError` so the operator is forced to pick a different
    judge — this prevents self-preference bias.
    """

    OPENAI = "openai"  # gpt-3.5/4/4o/o1/…
    ANTHROPIC = "anthropic"  # claude-*
    META = "meta"  # llama / llama3
    GOOGLE = "google"  # gemini / palm / flan
    MISTRAL = "mistral"  # mistral / mixtral
    QWEN = "qwen"  # qwen / qwen2 / qwen3
    DEEPSEEK = "deepseek"  # deepseek-*
    OLLAMA = "ollama"  # local ollama served model (unknown family)
    COHERE = "cohere"  # command-r
    AZURE = "azure"  # azure openai
    UNKNOWN = "unknown"


# Keyword → family mapping. Checked in order; the first hit wins so the
# more specific prefixes must come first.
_FAMILY_RULES: list[tuple[str, JudgeFamily]] = [
    ("gpt", JudgeFamily.OPENAI),
    ("o1", JudgeFamily.OPENAI),
    ("o3", JudgeFamily.OPENAI),
    ("o4", JudgeFamily.OPENAI),
    ("text-davinci", JudgeFamily.OPENAI),
    ("claude", JudgeFamily.ANTHROPIC),
    ("llama", JudgeFamily.META),
    ("meta-llama", JudgeFamily.META),
    ("gemini", JudgeFamily.GOOGLE),
    ("palm", JudgeFamily.GOOGLE),
    ("flan", JudgeFamily.GOOGLE),
    ("mistral", JudgeFamily.MISTRAL),
    ("mixtral", JudgeFamily.MISTRAL),
    ("qwen", JudgeFamily.QWEN),
    ("deepseek", JudgeFamily.DEEPSEEK),
    ("command-r", JudgeFamily.COHERE),
    ("command", JudgeFamily.COHERE),
    ("ollama", JudgeFamily.OLLAMA),
    ("azure", JudgeFamily.AZURE),
]

#: The family the *judge* is configured as. Used by :class:`LLMJudge` to
#: assert it differs from the MUT family. Tests override this.
JUDGE_FAMILY: JudgeFamily = JudgeFamily.UNKNOWN


def detect_model_family(model: str) -> JudgeFamily:
    """Best-effort family detection from a model name / path.

    Lower-cases and substring-matches against :data:`_FAMILY_RULES`. Ollama
    tags (``ollama/qwen3:8b``) are split on ``/`` first so the family is
    read from the underlying model, not the host prefix — except when the
    model name is literally ``ollama`` (host-only alias).
    """
    if not model:
        return JudgeFamily.UNKNOWN
    name = model.lower().strip()
    # ``provider/model`` form: prefer the model part for family detection,
    # but keep ``ollama`` as its own bucket when it's the only token.
    parts = name.split("/")
    candidates = parts[1:] if len(parts) > 1 else parts
    for cand in candidates:
        for needle, fam in _FAMILY_RULES:
            if needle in cand:
                return fam
    # Fallback: re-scan the whole name (catches ``gpt-4o-mini`` etc.).
    for needle, fam in _FAMILY_RULES:
        if needle in name:
            return fam
    return JudgeFamily.UNKNOWN


# ---------------------------------------------------------------------------
# Judge verdict
# ---------------------------------------------------------------------------


class JudgeVerdict(BaseModel):
    """Outcome of an LLM-as-judge evaluation of a single answer.

    ``score`` ∈ [0, 1] is the aggregate clinical quality. ``correct`` is a
    boolean derived from ``score >= 0.6`` (the threshold the MedQA /
    PubMedQA leaderboard convention treats as "acceptable"). ``reasoning``
    is the judge's free-text justification (auditable).
    """

    score: float = Field(ge=0.0, le=1.0)
    correct: bool
    citation_present: bool = False
    safety_concern: bool = False
    reasoning: str = ""
    fallback: bool = False  # True when scored by the deterministic fallback.


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


# Type alias for the chat callable. Mirrors the LLM provider protocol used
# across the clinical agents: ``chat_completion_sync(messages, **kw) -> str``
# plus an optional async ``chat_completion``.
ChatFn = Callable[..., str]
AsyncChatFn = Callable[..., Awaitable[str]]


class LLMJudge:
    """LLM-as-judge with a deterministic fallback.

    .. warning:: **PHI safety.** When ``chat``/``achat`` are configured the
        judge sends the case ``context`` + the model's prediction to an
        external LLM. The public MedQA / PubMedQA datasets carry no real
        patient PHI, so this is safe for standard benchmarking. When the
        benchmark is pointed at **real patient data**, the caller MUST
        de-identify cases first (e.g. via
        :class:`~doctoragent.clinical.deidentification.PHIDetector`) or
        keep the judge unconfigured so the deterministic fallback scorer
        runs locally (no network).

    Parameters
    ----------
    judge_family:
        The model family of the judge. When :attr:`enforce_cross_family`
        is set, :meth:`judge` raises if the MUT family equals this.
    model_name:
        Underlying judge model name (used only for logging / report).
    chat:
        Synchronous chat-completion callable. ``None`` → fallback scorer.
    achat:
        Asynchronous chat-completion callable. Preferred over ``chat`` when
        both are set (the runner is async).
    enforce_cross_family:
        When ``True`` (default), judging a MUT of the same family raises
        ``ValueError``. Set to ``False`` in single-vendor offline tests.
    """

    def __init__(
        self,
        *,
        judge_family: JudgeFamily = JUDGE_FAMILY,
        model_name: str = "judge",
        chat: ChatFn | None = None,
        achat: AsyncChatFn | None = None,
        enforce_cross_family: bool = True,
    ) -> None:
        self.judge_family = judge_family
        self.model_name = model_name
        self._chat = chat
        self._achat = achat
        self.enforce_cross_family = enforce_cross_family

    # ------------------------------------------------------------------
    # Cross-family enforcement
    # ------------------------------------------------------------------
    def assert_cross_family(self, mut_model: str) -> None:
        """Raise if the judge and MUT share a model family."""
        if not self.enforce_cross_family:
            return
        mut_family = detect_model_family(mut_model)
        if mut_family is not JudgeFamily.UNKNOWN and mut_family == self.judge_family:
            raise ValueError(
                f"cross-family judging violated: judge family {self.judge_family!r} "
                f"== model-under-test family {mut_family!r} (model={mut_model!r}). "
                "Pick a judge from a different family to avoid self-preference bias."
            )

    # ------------------------------------------------------------------
    # Judge prompt
    # ------------------------------------------------------------------
    _PROMPT_SYSTEM = (
        "You are a board-certified clinical examiner scoring a candidate's "
        "answer. Score ONLY clinical correctness and evidentiary support. "
        "Respond with STRICT JSON: "
        '{"score": <0..1>, "correct": <bool>, '
        '"citation_present": <bool>, "safety_concern": <bool>, '
        '"reasoning": "<one sentence>"}. '
        "Do not add any prose outside the JSON object."
    )

    def _build_messages(self, case: dict[str, Any], prediction: str) -> list[dict[str, str]]:
        gold = case.get("answer") or ""
        options = case.get("options") or []
        opts_str = ""
        if options:
            letters = [chr(ord("A") + i) for i in range(len(options))]
            opts_str = "\n".join(
                f"{letter}. {opt}" for letter, opt in zip(letters, options, strict=False)
            )
        ctx = case.get("context") or ""
        user = json.dumps(
            {
                "question": case.get("question", ""),
                "options": opts_str,
                "context": ctx[:2000],  # truncate to keep the prompt bounded
                "gold_answer": gold,
                "gold_rationale": (case.get("rationale") or "")[:500],
                "candidate_answer": prediction[:2000],
            },
            ensure_ascii=False,
        )
        return [
            {"role": "system", "content": self._PROMPT_SYSTEM},
            {"role": "user", "content": user},
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def judge(
        self, case: dict[str, Any], prediction: str, mut_model: str = "mut"
    ) -> JudgeVerdict:
        """Score ``prediction`` against the gold ``case``.

        Uses the async chat when available, else the sync chat, else the
        deterministic fallback scorer. The MUT family is checked against
        the judge family before any call.
        """
        self.assert_cross_family(mut_model)
        if self._achat is not None:
            raw = await self._achat(self._build_messages(case, prediction))
        elif self._chat is not None:
            raw = self._chat(self._build_messages(case, prediction))
        else:
            return self._fallback_judge(case, prediction)
        return self._parse_judge_response(raw)

    def judge_sync(
        self, case: dict[str, Any], prediction: str, mut_model: str = "mut"
    ) -> JudgeVerdict:
        """Synchronous variant for non-async callers."""
        self.assert_cross_family(mut_model)
        if self._chat is not None:
            raw = self._chat(self._build_messages(case, prediction))
        else:
            return self._fallback_judge(case, prediction)
        return self._parse_judge_response(raw)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_judge_response(self, raw: str) -> JudgeVerdict:
        """Extract a :class:`JudgeVerdict` from the judge's JSON-ish reply.

        Judges occasionally wrap JSON in prose or markdown fences; we find
        the first ``{...}`` block and parse it leniently.
        """
        if not raw or not raw.strip():
            return JudgeVerdict(
                score=0.0, correct=False, reasoning="empty judge response", fallback=True
            )
        from doctoragent._utils import extract_json

        # Robust centralised extractor (fenced blocks + bare spans) first.
        data = extract_json(raw)
        if data is None:
            # Last-resort leniency: strip single quotes and trailing commas.
            try:
                repaired = raw.replace("'", '"')
                repaired = re.sub(r",\s*}", "}", repaired)
                repaired = re.sub(r",\s*]", "]", repaired)
                data = json.loads(repaired)
            except json.JSONDecodeError:
                logger.warning("judge returned non-JSON: %r", raw[:200])
                return JudgeVerdict(
                    score=0.0,
                    correct=False,
                    reasoning="judge response not parseable",
                    fallback=True,
                )
        if not isinstance(data, dict):
            logger.warning("judge returned non-object JSON: %r", raw[:200])
            return JudgeVerdict(
                score=0.0,
                correct=False,
                reasoning="judge response not parseable",
                fallback=True,
            )
        score = float(data.get("score", 0.0))
        score = max(0.0, min(1.0, score))
        correct = bool(data.get("correct", score >= 0.6))
        return JudgeVerdict(
            score=score,
            correct=correct,
            citation_present=bool(data.get("citation_present", False)),
            safety_concern=bool(data.get("safety_concern", False)),
            reasoning=str(data.get("reasoning", "")),
        )

    # ------------------------------------------------------------------
    # Deterministic fallback (no LLM)
    # ------------------------------------------------------------------
    def _fallback_judge(self, case: dict[str, Any], prediction: str) -> JudgeVerdict:
        """Semantic-overlap scorer used when no LLM judge is configured.

        Token-Jaccard of the prediction against the gold answer + rationale,
        combined with a citation-presence heuristic. ``fallback=True`` so the
        report flags that LLM judging was unavailable.
        """
        gold = " ".join(filter(None, [str(case.get("answer", "")), str(case.get("rationale", ""))]))
        pred = prediction or ""
        score = _token_jaccard(pred, gold)
        citation_present = _has_citation(pred)
        # Safety concern heuristic: hedged language is fine; definitive dx
        # without context is flagged.
        safety_concern = bool(
            re.search(r"\b确诊(为|是)\b", pred) or re.search(r"\bdiagnosed with\b", pred, re.I)
        )
        correct = score >= 0.3  # laxer threshold for the overlap fallback
        return JudgeVerdict(
            score=score,
            correct=correct,
            citation_present=citation_present,
            safety_concern=safety_concern,
            reasoning="deterministic fallback: token-Jaccard vs gold",
            fallback=True,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _token_jaccard(a: str, b: str) -> float:
    """Word-level Jaccard similarity, lower-cased and punctuation-stripped."""

    def toks(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 1}

    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _has_citation(text: str) -> bool:
    return bool(
        re.search(r"\bPMID:?\s*\d+", text, re.I)
        or re.search(r"\bdoi:?\s*10\.\d{4,}/\S+", text, re.I)
        or re.search(r"\b[A-Z][a-zA-Z]+/\d[\w.-]*\b", text)
        or re.search(r"指南\s*[:：]\s*\S", text)
        or re.search(r"\b(?:NICE|WHO|FDA|CDC|AHA|ACC|ADA|ESC|GINA)\b", text)
    )
