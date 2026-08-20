"""Corrective RAG (CRAG) self-correction for DoctorAgent.

Implements the CRAG pattern: retrieved documents are graded for relevance,
and when they are insufficient the query is rewritten and retrieval is
repeated. Ambiguous retrievals trigger mixing local sources with
additional sources (e.g. web search), while clearly-incorrect retrievals
signal that a web search is warranted.

Pipeline::

    retrieve(query)
        -> evaluate_retrieval(query, docs)        # Correct / Incorrect / Ambiguous
        -> if Incorrect: rewrite_query + re-retrieve (up to max_iterations)
        -> if Ambiguous: mix_sources flag set for the caller
        -> return corrected results

LLM responses are parsed with the ``_extract_json`` helper from
:mod:`doctoragent.model.agent`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from doctoragent._utils import tokenize_words
from doctoragent.compat import StrEnum
from doctoragent.model.agent import _extract_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_response_text(response: Any) -> str:
    """Delegate to the shared :func:`normalize_llm_response`."""
    from doctoragent.model.provider import normalize_llm_response

    return normalize_llm_response(response)


def _doc_text(doc: Any) -> str:
    """Delegate to the shared :func:`extract_doc_text`."""
    from doctoragent._utils import extract_doc_text

    return extract_doc_text(doc)


def _doc_id(doc: Any) -> str:
    """Delegate to the shared :func:`extract_doc_id`."""
    from doctoragent._utils import extract_doc_id

    return extract_doc_id(doc)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class RetrievalAssessment(StrEnum):
    """Three-way relevance verdict for a set of retrieved documents."""

    CORRECT = "Correct"
    INCORRECT = "Incorrect"
    AMBIGUOUS = "Ambiguous"


@dataclass
class RetrievalEvaluation:
    """Outcome of evaluating a retrieval set against a query.

    ``score`` is a relevance score in ``[0, 1]``; ``assessment`` is the
    categorical verdict; ``reason`` is the LLM's (or heuristic's)
    justification.
    """

    score: float = 0.0
    assessment: RetrievalAssessment = RetrievalAssessment.AMBIGUOUS
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "score": float(self.score),
            "assessment": str(self.assessment),
            "reason": self.reason,
        }


@dataclass
class DocumentGrade:
    """Relevance grade for a single document."""

    relevant: bool = False
    score: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "relevant": self.relevant,
            "score": float(self.score),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Corrective RAG
# ---------------------------------------------------------------------------


class CorrectiveRAG:
    """Corrective RAG self-correction controller.

    The class is stateless beyond the optional ``llm_provider``; every
    method accepts the provider it needs so callers can swap providers per
    call.
    """

    def __init__(self, llm_provider: Any | None = None) -> None:
        self.llm_provider = llm_provider

    # ------------------------------------------------------------------
    # Retrieval evaluation
    # ------------------------------------------------------------------

    def evaluate_retrieval(
        self,
        query: str,
        retrieved_docs: list[Any],
        llm_provider: Any | None = None,
    ) -> RetrievalEvaluation:
        """Evaluate whether *retrieved_docs* are relevant to *query*.

        Returns a :class:`RetrievalEvaluation` with a score, a three-way
        assessment and a reason. When no LLM is available a conservative
        heuristic is used: non-empty retrievals are marked ``Ambiguous``
        while empty ones are marked ``Incorrect``.
        """
        if not retrieved_docs:
            return RetrievalEvaluation(
                score=0.0,
                assessment=RetrievalAssessment.INCORRECT,
                reason="No documents were retrieved.",
            )

        provider = llm_provider or self.llm_provider
        if provider is None:
            return self._heuristic_evaluation(query, retrieved_docs)

        docs_block = self._format_docs(retrieved_docs)
        prompt = (
            "You are a retrieval evaluator. Decide whether the retrieved "
            "documents are sufficient to answer the user's query.\n\n"
            "Return ONLY a JSON object:\n"
            '{"score": <0.0-1.0>, "assessment": "Correct|Incorrect|Ambiguous", '
            '"reason": "<short justification>"}\n\n'
            f"Query: {query}\n\n"
            f"Retrieved documents:\n{docs_block}\n"
        )
        try:
            messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            response = provider.chat_completion_sync(messages)
            data = _extract_json(_llm_response_text(response))
        except Exception as exc:  # noqa: BLE001
            logger.warning("CRAG retrieval evaluation failed: %s", exc)
            return self._heuristic_evaluation(query, retrieved_docs)

        if not isinstance(data, dict):
            return self._heuristic_evaluation(query, retrieved_docs)

        try:
            score = float(data.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))

        assessment = self._parse_assessment(data.get("assessment"))
        reason = str(data.get("reason") or "")
        return RetrievalEvaluation(score=score, assessment=assessment, reason=reason)

    def _heuristic_evaluation(
        self,
        query: str,
        retrieved_docs: list[Any],
    ) -> RetrievalEvaluation:
        """Cheap fallback when no LLM is available.

        Scores by simple lexical overlap between the query and the retrieved
        documents. This is intentionally conservative: it never returns
        ``Correct`` so the caller is encouraged to supply an LLM.
        """
        # Use the shared jieba-backed tokeniser so Chinese clinical queries
        # are segmented into words rather than treated as one unsplit CJK
        # blob (which would make the lexical-overlap fallback degenerate to
        # all-or-nothing matching for non-ASCII content).
        query_terms = {t for t in tokenize_words(query) if len(t) > 2}
        if not query_terms:
            return RetrievalEvaluation(
                score=0.0,
                assessment=RetrievalAssessment.INCORRECT,
                reason="Query has no usable search terms.",
            )
        best_overlap = 0.0
        for doc in retrieved_docs:
            text = _doc_text(doc).lower()
            if not text:
                continue
            hits = sum(1 for term in query_terms if term in text)
            overlap = hits / len(query_terms)
            best_overlap = max(best_overlap, overlap)
        if best_overlap >= 0.5:
            assessment = RetrievalAssessment.AMBIGUOUS
            reason = "Heuristic: moderate lexical overlap (no LLM available)."
        else:
            assessment = RetrievalAssessment.INCORRECT
            reason = "Heuristic: low lexical overlap (no LLM available)."
        return RetrievalEvaluation(
            score=round(best_overlap, 4),
            assessment=assessment,
            reason=reason,
        )

    @staticmethod
    def _parse_assessment(value: Any) -> RetrievalAssessment:
        """Parse a free-form assessment string into an enum member."""
        if isinstance(value, RetrievalAssessment):
            return value
        text = str(value or "").strip().lower()
        if text.startswith("correct"):
            return RetrievalAssessment.CORRECT
        if text.startswith("incorrect"):
            return RetrievalAssessment.INCORRECT
        if text.startswith("ambiguous"):
            return RetrievalAssessment.AMBIGUOUS
        return RetrievalAssessment.AMBIGUOUS

    # ------------------------------------------------------------------
    # Routing decisions
    # ------------------------------------------------------------------

    def should_web_search(self, evaluation: RetrievalEvaluation) -> bool:
        """Return ``True`` when local retrieval is clearly insufficient."""
        return evaluation.assessment == RetrievalAssessment.INCORRECT

    def should_mix_sources(self, evaluation: RetrievalEvaluation) -> bool:
        """Return ``True`` when local retrieval is ambiguous.

        In the ambiguous case the caller should augment local documents
        with additional (e.g. web) sources rather than replacing them.
        """
        return evaluation.assessment == RetrievalAssessment.AMBIGUOUS

    # ------------------------------------------------------------------
    # Per-document grading
    # ------------------------------------------------------------------

    def grade_document(
        self,
        query: str,
        doc: Any,
        llm_provider: Any | None = None,
    ) -> DocumentGrade:
        """Grade a single *doc* as relevant or irrelevant to *query*.

        Returns a :class:`DocumentGrade` with ``relevant`` flag, a
        ``[0, 1]`` score and a reason. Falls back to a lexical-overlap
        heuristic when no LLM is available.
        """
        provider = llm_provider or self.llm_provider
        text = _doc_text(doc)
        if not text.strip():
            return DocumentGrade(relevant=False, score=0.0, reason="Empty document.")

        if provider is None:
            # Use the shared jieba-backed tokeniser so Chinese clinical queries
            # are segmented into words rather than treated as one unsplit CJK
            # blob (which would make the lexical-overlap fallback degenerate to
            # all-or-nothing matching for non-ASCII content).
            query_terms = {t for t in tokenize_words(query) if len(t) > 2}
            if not query_terms:
                return DocumentGrade(relevant=False, score=0.0, reason="No query terms.")
            doc_lower = text.lower()
            hits = sum(1 for term in query_terms if term in doc_lower)
            score = hits / len(query_terms)
            return DocumentGrade(
                relevant=score >= 0.4,
                score=round(score, 4),
                reason="Heuristic lexical overlap.",
            )

        prompt = (
            "Grade whether the document is relevant to the query.\n\n"
            "Return ONLY a JSON object:\n"
            '{"relevant": true|false, "score": <0.0-1.0>, '
            '"reason": "<short>"}\n\n'
            f"Query: {query}\n\n"
            f"Document: {text[:2000]}\n"
        )
        try:
            messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            response = provider.chat_completion_sync(messages)
            data = _extract_json(_llm_response_text(response))
        except Exception as exc:  # noqa: BLE001
            logger.warning("CRAG document grading failed: %s", exc)
            return DocumentGrade(relevant=False, score=0.0, reason="LLM grading error.")

        if not isinstance(data, dict):
            return DocumentGrade(relevant=False, score=0.0, reason="Unparseable LLM output.")
        try:
            score = float(data.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        relevant = bool(data.get("relevant", score >= 0.5))
        reason = str(data.get("reason") or "")
        return DocumentGrade(relevant=relevant, score=score, reason=reason)

    # ------------------------------------------------------------------
    # Query rewriting
    # ------------------------------------------------------------------

    def rewrite_query(
        self,
        query: str,
        llm_provider: Any | None = None,
    ) -> str:
        """Rewrite *query* for better retrieval.

        The LLM is asked to produce a clearer, more search-friendly
        reformulation that preserves the original intent. Returns the
        original query unchanged when no LLM is available or rewriting
        fails.
        """
        provider = llm_provider or self.llm_provider
        if provider is None:
            return query

        prompt = (
            "Rewrite the following query to improve retrieval. Make it "
            "clearer and more specific while preserving the original "
            "intent. Do not add information that is not implied by the "
            "query. Output ONLY the rewritten query, nothing else.\n\n"
            f"Original query: {query}\n\n"
            "Rewritten query:"
        )
        try:
            messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            response = provider.chat_completion_sync(messages)
            rewritten = _llm_response_text(response).strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CRAG query rewrite failed: %s", exc)
            return query
        return rewritten or query

    # ------------------------------------------------------------------
    # Correction loop
    # ------------------------------------------------------------------

    def run_correction_loop(
        self,
        query: str,
        retrieve_fn: Callable[[str], list[Any]],
        llm_provider: Any | None = None,
        max_iterations: int = 2,
    ) -> dict[str, Any]:
        """Run the full CRAG correction loop.

        Args:
            query: The original user query.
            retrieve_fn: A synchronous callable ``(query) -> list[docs]``.
            llm_provider: Optional provider override.
            max_iterations: Maximum number of rewrite/re-retrieve rounds.

        The loop:

        1. Retrieve documents for the current query.
        2. Evaluate the retrieval. If ``Correct``, return immediately.
        3. Otherwise rewrite the query and re-retrieve (up to
           ``max_iterations`` times).
        4. Return the best result set found along with the final evaluation
           and the action trace.

        Returns a dict with ``query``, ``docs``, ``evaluation``,
        ``iterations``, ``corrected`` and ``trace``.
        """
        provider = llm_provider or self.llm_provider
        current_query = query
        trace: list[dict[str, Any]] = []

        docs = self._safe_retrieve(retrieve_fn, current_query)
        evaluation = self.evaluate_retrieval(current_query, docs, provider)
        trace.append(
            {
                "iteration": 0,
                "query": current_query,
                "doc_count": len(docs),
                "assessment": str(evaluation.assessment),
            }
        )

        iterations = 0
        corrected = False
        for i in range(1, max_iterations + 1):
            if evaluation.assessment == RetrievalAssessment.CORRECT:
                break

            corrected = True
            rewritten = self.rewrite_query(current_query, provider)
            if not rewritten or rewritten == current_query:
                # No useful rewrite possible; stop to avoid a tight loop.
                logger.debug("CRAG rewrite produced no change; stopping.")
                break
            current_query = rewritten

            docs = self._safe_retrieve(retrieve_fn, current_query)
            evaluation = self.evaluate_retrieval(current_query, docs, provider)
            iterations = i
            trace.append(
                {
                    "iteration": i,
                    "query": current_query,
                    "doc_count": len(docs),
                    "assessment": str(evaluation.assessment),
                }
            )

        return {
            "query": current_query,
            "original_query": query,
            "docs": docs,
            "evaluation": evaluation.to_dict(),
            "iterations": iterations,
            "corrected": corrected,
            "trace": trace,
            "web_search": self.should_web_search(evaluation),
            "mix_sources": self.should_mix_sources(evaluation),
        }

    @staticmethod
    def _safe_retrieve(
        retrieve_fn: Callable[[str], list[Any]],
        query: str,
    ) -> list[Any]:
        """Call *retrieve_fn* defensively, returning ``[]`` on error."""
        try:
            result = retrieve_fn(query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CRAG retrieve_fn raised: %s", exc)
            return []
        if result is None:
            return []
        return list(result)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_docs(docs: list[Any], max_docs: int = 8, max_chars: int = 600) -> str:
        """Render a compact, length-bounded view of the retrieved docs."""
        if not docs:
            return "(no documents)"
        lines: list[str] = []
        for idx, doc in enumerate(docs[:max_docs]):
            text = _doc_text(doc)
            if len(text) > max_chars:
                text = text[:max_chars] + "..."
            did = _doc_id(doc)
            header = f"[{idx}]" + (f" id={did}" if did else "")
            lines.append(f"{header}\n{text}")
        if len(docs) > max_docs:
            lines.append(f"... ({len(docs) - max_docs} more documents)")
        return "\n\n".join(lines)
