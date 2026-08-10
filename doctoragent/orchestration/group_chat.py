"""Multi-agent group chat orchestration (M6.4).

An AutoGen-style round-robin conversation among several agents plus a manager.
Each agent is a callable ``(message, context) -> str`` (e.g. an adapter's
``run`` or a plain function). The manager routes turns; agents speak in order
(or are selected by the manager) until a stop condition or max turns.

Real, testable implementation — no framework SDK required.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

AgentFn = Callable[[str, dict[str, Any]], Awaitable[str] | str]

STOP_PREFIXES = ("[STOP]", "[DONE]", "会议结束", "总结：")


class GroupChatAgent:
    """A participant in the group chat."""

    def __init__(self, name: str, role: str, fn: AgentFn, system: str = "") -> None:
        self.name = name
        self.role = role
        self.fn = fn
        self.system = system
        self.messages: list[dict[str, Any]] = []

    async def speak(self, topic: str, context: dict[str, Any]) -> str:
        prompt = self.system + "\n" if self.system else ""
        prompt += f"[本轮主题] {topic}\n[上下文] {context.get('summary', '')}\n请发言："
        if asyncio.iscoroutinefunction(self.fn):
            text = await self.fn(prompt, context)
        else:
            text = self.fn(prompt, context)
        text = str(text)
        self.messages.append({"role": self.name, "content": text})
        return text


class GroupChatManager:
    """Round-robin group conversation coordinator (M6.4)."""

    def __init__(self, max_turns: int = 8, max_speaker_chars: int = 800) -> None:
        self.max_turns = max_turns
        self.max_speaker_chars = max_speaker_chars

    async def run(
        self,
        topic: str,
        agents: list[GroupChatAgent],
        *,
        start_with: int = 0,
        manager_fn: AgentFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the group chat and return the transcript + final summary.

        Args:
            topic: the discussion topic.
            agents: ordered list of participants.
            start_with: index of the first speaker.
            manager_fn: optional manager that picks the next speaker index
                from the latest transcript (``(topic, history) -> int``).
            context: shared context dict passed to each speaker.
        """
        context = dict(context or {})
        transcript: list[dict[str, Any]] = []
        idx = start_with
        speaker_messages: list[str] = []
        stop = False

        for turn in range(self.max_turns):
            agent = agents[idx % len(agents)]
            try:
                text = await agent.speak(topic, context)
            except Exception as exc:  # noqa: BLE001
                logger.warning("group chat speaker %s failed: %s", agent.name, exc)
                text = f"[{agent.name} 发言失败: {exc}]"
            transcript.append({"speaker": agent.name, "role": agent.role, "content": text})
            speaker_messages.append(text)

            # Manager may choose the next speaker from the latest transcript.
            if manager_fn is not None:
                try:
                    nxt = manager_fn(topic, transcript)
                    if isinstance(nxt, int):
                        idx = nxt % len(agents)
                    else:
                        idx = (idx + 1) % len(agents)
                except Exception:  # noqa: BLE001 — fall back to round-robin
                    idx = (idx + 1) % len(agents)
            else:
                idx = (idx + 1) % len(agents)

            # Summarize incremental context for the next speaker.
            context["summary"] = "\n".join(
                f"{t['speaker']}: {t['content'][:self.max_speaker_chars]}"
                for t in transcript[-4:]
            )

            if any(t["content"].startswith(prefix) for t in transcript[-1:] for prefix in STOP_PREFIXES):
                stop = True
                break

        # Final summary = last manager-eligible text or last speaker.
        summary = self._summarize(transcript)
        return {
            "topic": topic,
            "turns": len(transcript),
            "stopped": stop,
            "transcript": transcript,
            "summary": summary,
        }

    @staticmethod
    def _summarize(transcript: list[dict[str, Any]]) -> str:
        if not transcript:
            return ""
        lines = [
            f"{t['speaker']}({t['role']}): {t['content']}"
            for t in transcript
        ]
        return "\n".join(lines[-8:])


def run_debate(
    topic: str,
    pro_fn: AgentFn,
    con_fn: AgentFn,
    judge_fn: AgentFn,
    *,
    rounds: int = 2,
    manager: GroupChatManager | None = None,
) -> dict[str, Any]:
    """Run a debate (M6.19): pro vs con agents argue, then a judge rules.

    Real implementation on top of the group chat. Args are plain callables
    (sync or async). Returns the transcript and the judge's verdict.
    """
    import asyncio

    manager = manager or GroupChatManager(max_turns=rounds * 2 + 1)
    pro = GroupChatAgent("正方", "pro", pro_fn, "你是正方辩手，坚持支持立场，给出有据的论证。")
    con = GroupChatAgent("反方", "con", con_fn, "你是反方辩手，坚持反对立场，指出风险与反驳。")
    judge = GroupChatAgent("裁判", "judge", judge_fn,
                           "你是中立裁判，综合双方论点，给出最终裁决并说明理由。")

    async def _run() -> dict[str, Any]:
        transcript: list[dict[str, Any]] = []
        for rnd in range(rounds):
            for agent in (pro, con):
                try:
                    text = await agent.speak(topic, {"summary": _tail(transcript)})
                except Exception as exc:  # noqa: BLE001
                    text = f"[{agent.name} 发言失败: {exc}]"
                transcript.append({"speaker": agent.name, "role": agent.role, "content": text})
        verdict = await judge.speak(topic, {"summary": _tail(transcript, 6)})
        transcript.append({"speaker": "裁判", "role": "judge", "content": verdict})
        return {"topic": topic, "rounds": rounds, "transcript": transcript, "verdict": verdict}

    return asyncio.run(_run())


def _tail(transcript: list[dict[str, Any]], n: int = 4) -> str:
    return "\n".join(f"{t['speaker']}: {t['content']}" for t in transcript[-n:])


def build_group_chat_agents(
    participants: list[dict[str, Any]],
    run_fn: AgentFn,
) -> list[GroupChatAgent]:
    """Build :class:`GroupChatAgent` list from declarative participant specs.

    Each ``participant`` dict: ``{name, role, system?}``. All share *run_fn*.
    """
    return [
        GroupChatAgent(
            p.get("name", f"agent_{i}"),
            p.get("role", "member"),
            run_fn,
            p.get("system", ""),
        )
        for i, p in enumerate(participants)
    ]
