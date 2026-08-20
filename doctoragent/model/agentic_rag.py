"""Agentic RAG for DoctorAgent.

Instead of a fixed retrieve-then-generate pipeline, the LLM acts as a
controller that decides which retrieval action to take next (retrieve,
rewrite the query, view a full document, search the knowledge graph,
generate, or terminate) based on the accumulated state. This yields more
targeted, multi-step retrieval for complex questions.

The controller loop::

    state = AgenticRAGState(query)
    while not terminated and iteration < max_iterations:
        decision = decide_action(state)      # LLM picks the next action
        execute_action(state, decision)      # perform it, mutate state
        record(decision)                     # action_history (observability)

The ``retrieve_fn`` is an ``async`` callable ``(query, top_k) -> list[docs]``
so the agent can await real (potentially async) retrievers.

LLM responses are parsed with the ``_extract_json`` helper from
:mod:`doctoragent.model.agent`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from doctoragent.compat import StrEnum
from doctoragent.model.agent import _extract_json

logger = logging.getLogger(__name__)


# Type alias for the async retrieval callable.
RetrieveFn = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _acall_llm(llm: Any, messages: list[dict[str, Any]]) -> str:
    """Call an LLM provider and return its text response.

    Prefers the async ``chat_completion`` API (all built-in providers
    expose it); falls back to the synchronous ``chat_completion_sync``
    wrapper when only that is available. Returns ``""`` on failure.
    """
    ac = getattr(llm, "chat_completion", None)
    if ac is not None:
        try:
            result = ac(messages)
            if asyncio.iscoroutine(result):
                result = await result
            return _llm_response_text(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agentic RAG async LLM call failed: %s", exc)
            if getattr(llm, "chat_completion_sync", None) is None:
                return ""
    sc = getattr(llm, "chat_completion_sync", None)
    if sc is not None:
        try:
            return _llm_response_text(sc(messages))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agentic RAG sync LLM call failed: %s", exc)
    return ""


def _llm_response_text(response: Any) -> str:
    """Delegate to the shared :func:`normalize_llm_response`."""
    from doctoragent.model.provider import normalize_llm_response

    return normalize_llm_response(response)


def _doc_text(doc: Any, max_chars: int = 400) -> str:
    """Delegate to the shared :func:`extract_doc_text`."""
    from doctoragent._utils import extract_doc_text

    return extract_doc_text(doc, max_chars=max_chars)


def _doc_ref(doc: Any) -> str:
    """Delegate to the shared :func:`extract_doc_id`."""
    from doctoragent._utils import extract_doc_id

    return extract_doc_id(doc)


# ---------------------------------------------------------------------------
# Action model
# ---------------------------------------------------------------------------


class RAGAction(StrEnum):
    """Actions the agentic RAG controller can choose between."""

    RETRIEVE = "retrieve"
    GENERATE = "generate"
    REWRITE_QUERY = "rewrite_query"
    VIEW_FULL_DOC = "view_full_doc"
    SEARCH_GRAPH = "search_graph"
    TERMINATE = "terminate"


@dataclass
class ActionDecision:
    """A single controller decision.

    ``action`` is the chosen :class:`RAGAction`; ``reason`` is the LLM's
    justification (kept for observability); ``params`` carries action
    parameters such as ``top_k`` for ``RETRIEVE`` or ``doc_ref`` /
    ``query`` for ``VIEW_FULL_DOC`` / ``SEARCH_GRAPH``.
    """

    action: RAGAction
    reason: str = ""
    params: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the action history trail."""
        return {
            "action": str(self.action),
            "reason": self.reason,
            "params": dict(self.params),
        }


@dataclass
class AgenticRAGState:
    """Mutable state carried through the agentic RAG loop.

    Attributes:
        query: The current (possibly rewritten) query.
        retrieved_docs: Documents gathered so far.
        current_answer: The latest generated answer (if any).
        iteration: Number of executed iterations.
        action_history: Ordered record of every decision taken.
        messages: The LLM conversation transcript (system + turns).
    """

    query: str
    retrieved_docs: list[dict[str, Any]] = dc_field(default_factory=list)
    current_answer: str = ""
    iteration: int = 0
    action_history: list[dict[str, Any]] = dc_field(default_factory=list)
    messages: list[dict[str, Any]] = dc_field(default_factory=list)


# ---------------------------------------------------------------------------
# Agentic RAG controller
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an agentic retrieval controller. Based on the current state you "
    "decide the single next action to take to answer the user's question.\n\n"
    "Available actions:\n"
    '- retrieve: fetch more documents (params: {"top_k": int})\n'
    "- rewrite_query: rewrite the query for better retrieval "
    '(params: {"query": str})\n'
    "- view_full_doc: load the full text of a specific retrieved document "
    '(params: {"doc_ref": str})\n'
    "- search_graph: query the knowledge graph for related entities "
    '(params: {"query": str})\n'
    "- generate: produce the final answer from the current context\n"
    "- terminate: stop the loop (use after generating an answer or when "
    "stuck)\n\n"
    "Return ONLY a JSON object: "
    '{"action": "<action>", "reason": "<short>", "params": {...}}'
)


class AgenticRAG:
    """Agentic RAG controller.

    Args:
        retrieve_fn: Async callable ``(query, top_k) -> list[docs]`` used by
            the ``RETRIEVE`` action (and as a fallback for the specialised
            actions when no dedicated handler is attached).
        llm_provider: Provider used for deciding actions and generating
            answers.
        max_iterations: Hard cap on the number of actions executed.
    """

    def __init__(
        self,
        retrieve_fn: RetrieveFn,
        llm_provider: Any | None,
        max_iterations: int = 5,
    ) -> None:
        self.retrieve_fn = retrieve_fn
        self.llm_provider = llm_provider
        self.max_iterations = max(1, max_iterations)
        # Optional specialised handlers. When ``None`` the corresponding
        # action falls back to ``retrieve_fn`` so the controller still
        # makes progress; attach real handlers for full fidelity.
        self.graph_search_fn: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None
        self.doc_loader_fn: Callable[[str], Awaitable[str | None]] | None = None

    # ------------------------------------------------------------------
    # Decision making
    # ------------------------------------------------------------------

    async def decide_action(
        self,
        state: AgenticRAGState,
        llm_provider: Any | None = None,
    ) -> ActionDecision:
        """Ask the LLM which action to take next.

        Falls back to a deterministic policy when no LLM is available:
        retrieve first (if no docs), then generate, then terminate.
        """
        provider = llm_provider or self.llm_provider
        if provider is None:
            return self._fallback_decision(state)

        context = self.format_state_for_llm(state)
        prompt = (
            f"{_SYSTEM_PROMPT}\n\nCurrent state:\n{context}\n\n"
            "Decide the next action. Return ONLY the JSON object."
        )
        messages = list(state.messages)
        messages.append({"role": "user", "content": prompt})

        text = await _acall_llm(provider, messages)
        data = _extract_json(text)
        if isinstance(data, dict):
            decision = self._parse_decision(data)
            if decision is not None:
                return decision
            logger.debug("Agentic RAG could not parse decision: %r", text)

        return self._fallback_decision(state)

    def _parse_decision(self, data: dict[str, Any]) -> ActionDecision | None:
        """Parse a JSON decision object into an :class:`ActionDecision`."""
        raw_action = str(data.get("action", "")).strip().lower()
        if not raw_action:
            return None
        try:
            action = RAGAction(raw_action)
        except ValueError:
            logger.debug("Agentic RAG unknown action: %r", raw_action)
            return None
        reason = str(data.get("reason", ""))
        params = data.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        # Coerce common numeric params.
        if "top_k" in params:
            try:
                params["top_k"] = int(params["top_k"])
            except (TypeError, ValueError):
                params.pop("top_k", None)
        return ActionDecision(action=action, reason=reason, params=dict(params))

    def _fallback_decision(self, state: AgenticRAGState) -> ActionDecision:
        """Deterministic policy when no LLM is available."""
        if not state.retrieved_docs:
            return ActionDecision(
                RAGAction.RETRIEVE,
                "No documents retrieved yet; fetching context.",
                {"top_k": 5},
            )
        if not state.current_answer:
            return ActionDecision(
                RAGAction.GENERATE,
                "Documents available; generating an answer.",
            )
        return ActionDecision(RAGAction.TERMINATE, "Answer produced; stopping.")

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def execute_action(
        self,
        state: AgenticRAGState,
        decision: ActionDecision,
    ) -> None:
        """Execute *decision*, mutating *state* in place."""
        action = decision.action
        if action == RAGAction.RETRIEVE:
            await self._do_retrieve(state, decision.params)
        elif action == RAGAction.REWRITE_QUERY:
            await self._do_rewrite(state, decision.params)
        elif action == RAGAction.VIEW_FULL_DOC:
            await self._do_view_doc(state, decision.params)
        elif action == RAGAction.SEARCH_GRAPH:
            await self._do_search_graph(state, decision.params)
        elif action == RAGAction.GENERATE:
            await self._do_generate(state, decision.params)
        elif action == RAGAction.TERMINATE:
            # Nothing to execute; the loop breaks on this action.
            pass
        else:  # pragma: no cover - defensive
            logger.warning("Agentic RAG unknown action executed: %s", action)

    async def _do_retrieve(
        self,
        state: AgenticRAGState,
        params: dict[str, Any],
    ) -> None:
        """Fetch additional documents and merge them into the state."""
        top_k = self._safe_int(params.get("top_k"), default=5)
        query = str(params.get("query") or state.query)
        try:
            docs = await self.retrieve_fn(query, top_k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agentic RAG retrieve failed: %s", exc)
            docs = []
        merged = self._merge_docs(state.retrieved_docs, docs)
        state.retrieved_docs = merged
        state.messages.append(
            {
                "role": "assistant",
                "content": f"Retrieved {len(docs)} document(s).",
            }
        )

    async def _do_rewrite(
        self,
        state: AgenticRAGState,
        params: dict[str, Any],
    ) -> None:
        """Rewrite the query (LLM-driven) and keep the original in history."""
        provider = self.llm_provider
        new_query = str(params.get("query") or "").strip()
        if not new_query and provider is not None:
            prompt = (
                "Rewrite the following query to improve retrieval while "
                "preserving intent. Output ONLY the rewritten query.\n\n"
                f"Query: {state.query}"
            )
            new_query = (await _acall_llm(provider, [{"role": "user", "content": prompt}])).strip()
        if new_query and new_query != state.query:
            state.action_history.append(
                {
                    "iteration": state.iteration,
                    "note": f"query rewritten: {state.query!r} -> {new_query!r}",
                }
            )
            state.query = new_query
        state.messages.append(
            {
                "role": "assistant",
                "content": f"Query rewritten to: {state.query}",
            }
        )

    async def _do_view_doc(
        self,
        state: AgenticRAGState,
        params: dict[str, Any],
    ) -> None:
        """Load the full text of a document for richer context."""
        doc_ref = str(params.get("doc_ref") or "").strip()
        if not doc_ref:
            # Pick the most recently retrieved document.
            for doc in reversed(state.retrieved_docs):
                ref = _doc_ref(doc)
                if ref:
                    doc_ref = ref
                    break
        if not doc_ref:
            logger.debug("Agentic RAG view_full_doc: no document reference.")
            return
        full_text: str | None = None
        if self.doc_loader_fn is not None:
            try:
                full_text = await self.doc_loader_fn(doc_ref)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Agentic RAG doc_loader_fn failed: %s", exc)
        if full_text:
            # Replace/append the full text onto the matching doc.
            updated = False
            for doc in state.retrieved_docs:
                if _doc_ref(doc) == doc_ref:
                    doc["text"] = full_text
                    doc["full_text_loaded"] = True
                    updated = True
                    break
            if not updated:
                state.retrieved_docs.append(
                    {
                        "doc_id": doc_ref,
                        "text": full_text,
                        "full_text_loaded": True,
                    }
                )
        state.messages.append(
            {
                "role": "assistant",
                "content": f"Viewed full document {doc_ref}.",
            }
        )

    async def _do_search_graph(
        self,
        state: AgenticRAGState,
        params: dict[str, Any],
    ) -> None:
        """Query the knowledge graph for related context."""
        query = str(params.get("query") or state.query)
        docs: list[dict[str, Any]] = []
        if self.graph_search_fn is not None:
            try:
                docs = await self.graph_search_fn(query)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Agentic RAG graph_search_fn failed: %s", exc)
                docs = []
        else:
            # Fallback: reuse the standard retriever as a graph proxy.
            try:
                docs = await self.retrieve_fn(query, 5)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Agentic RAG graph fallback retrieve failed: %s", exc)
                docs = []
            for doc in docs:
                doc["source"] = "graph_fallback"
        state.retrieved_docs = self._merge_docs(state.retrieved_docs, docs)
        state.messages.append(
            {
                "role": "assistant",
                "content": f"Knowledge graph search returned {len(docs)} document(s).",
            }
        )

    async def _do_generate(
        self,
        state: AgenticRAGState,
        params: dict[str, Any],
    ) -> None:
        """Produce the final answer from the gathered context."""
        provider = self.llm_provider
        if provider is None:
            # Without an LLM, synthesise a minimal answer from doc snippets.
            snippets = [_doc_text(d) for d in state.retrieved_docs[:3]]
            state.current_answer = "\n\n".join(s for s in snippets if s) or "(no answer)"
            state.messages.append({"role": "assistant", "content": state.current_answer})
            return

        context = self.format_state_for_llm(state)
        prompt = (
            "Answer the user's question using only the retrieved context "
            "below. If the context is insufficient, say so. Cite documents "
            "by their index when possible.\n\n"
            f"Question: {state.query}\n\n"
            f"Context:\n{context}\n\n"
            "Answer:"
        )
        messages = list(state.messages)
        messages.append({"role": "user", "content": prompt})
        answer = await _acall_llm(provider, messages)
        state.current_answer = answer.strip()
        state.messages.append({"role": "assistant", "content": state.current_answer})

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, query: str) -> AgenticRAGState:
        """Run the agentic RAG loop until ``TERMINATE`` or the iteration cap.

        The loop is a strict *decide -> execute -> repeat* cycle: the LLM
        picks the next action, the action is executed (mutating *state*),
        and the process repeats until the controller emits ``TERMINATE`` or
        ``max_iterations`` is reached.

        Returns the final :class:`AgenticRAGState` containing the answer
        (``current_answer``), the gathered documents and the full
        ``action_history`` for observability.
        """
        state = AgenticRAGState(query=query)
        state.messages = [{"role": "system", "content": _SYSTEM_PROMPT}]

        while state.iteration < self.max_iterations:
            decision = await self.decide_action(state)
            # Record the decision BEFORE execution so a failure is still
            # visible in the history.
            state.action_history.append(
                {
                    "iteration": state.iteration,
                    **decision.to_dict(),
                }
            )
            logger.debug(
                "Agentic RAG iter=%d action=%s reason=%s",
                state.iteration,
                decision.action,
                decision.reason,
            )

            if decision.action == RAGAction.TERMINATE:
                break

            await self.execute_action(state, decision)
            state.iteration += 1

        if state.iteration >= self.max_iterations:
            logger.info(
                "Agentic RAG hit max_iterations (%d) for query: %s",
                self.max_iterations,
                query,
            )
        return state

    # ------------------------------------------------------------------
    # State formatting
    # ------------------------------------------------------------------

    def format_state_for_llm(self, state: AgenticRAGState) -> str:
        """Render *state* as a compact text block for the LLM to reason over."""
        lines: list[str] = []
        lines.append(f"Query: {state.query}")
        lines.append(f"Iteration: {state.iteration}/{self.max_iterations}")
        lines.append(f"Documents retrieved: {len(state.retrieved_docs)}")
        if state.retrieved_docs:
            lines.append("Documents:")
            for idx, doc in enumerate(state.retrieved_docs[:8]):
                ref = _doc_ref(doc)
                snippet = _doc_text(doc, max_chars=200).replace("\n", " ")
                header = f"  [{idx}]" + (f" {ref}" if ref else "")
                lines.append(f"{header}: {snippet}")
            if len(state.retrieved_docs) > 8:
                lines.append(f"  ... ({len(state.retrieved_docs) - 8} more)")
        else:
            lines.append("Documents: (none yet)")
        if state.current_answer:
            preview = state.current_answer[:300]
            if len(state.current_answer) > 300:
                preview += "..."
            lines.append(f"Current answer: {preview}")
        if state.action_history:
            recent = state.action_history[-5:]
            lines.append("Recent actions:")
            for entry in recent:
                lines.append(
                    f"  - iter={entry.get('iteration')} "
                    f"{entry.get('action')}: {entry.get('reason', '')}"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_int(value: Any, default: int = 5, minimum: int = 1) -> int:
        """Coerce *value* to a positive int, falling back to *default*."""
        try:
            out = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, out)

    @staticmethod
    def _merge_docs(
        existing: list[dict[str, Any]],
        new: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge two doc lists, de-duplicating by document reference."""
        merged = list(existing)
        seen = {_doc_ref(d) for d in existing if _doc_ref(d)}
        for doc in new or []:
            ref = _doc_ref(doc)
            if ref and ref in seen:
                continue
            merged.append(doc)
            if ref:
                seen.add(ref)
        return merged
