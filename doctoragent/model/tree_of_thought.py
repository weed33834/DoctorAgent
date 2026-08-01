"""Tree-of-Thought (ToT) reasoning module.

Implements branching reasoning where multiple solution paths are explored
in parallel, evaluated, and the most promising path is selected.

Inspired by: Yao et al. "Tree of Thoughts: Deliberate Problem Solving with Large Language Models" (2023)

The module exposes three building blocks:

* :class:`ThoughtNode` / :class:`ThoughtTree` - the tree data structure that
  stores thoughts, their evaluation scores and parent/child relationships.
* :class:`TreeOfThoughts` - the search driver that uses an LLM to *generate*
  candidate next thoughts, *evaluate* them (0-1) and explore the tree using
  either breadth-first (beam) or depth-first search with pruning. Thought
  generation and evaluation run concurrently via :mod:`asyncio`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from doctoragent._utils import async_to_sync
from doctoragent.compat import StrEnum
from doctoragent.model.agent import _extract_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search strategy
# ---------------------------------------------------------------------------


class SearchStrategy(StrEnum):
    """Tree exploration strategy."""

    BFS = "bfs"  # breadth-first / beam search
    DFS = "dfs"  # depth-first, best-first descent


# ---------------------------------------------------------------------------
# Tree data structures
# ---------------------------------------------------------------------------


@dataclass
class ThoughtNode:
    """A single node in the thought tree.

    Attributes
    ----------
    id:
        Unique node identifier within its tree.
    thought:
        The reasoning text / proposed next step.
    evaluation_score:
        LLM-assessed promise of this thought in ``[0, 1]``.
    parent_id:
        Id of the parent node (``None`` for the root).
    children_ids:
        Ordered list of child node ids.
    state:
        Arbitrary state payload (typically a dict capturing the partial
        solution context accumulated along the path).
    depth:
        Depth of the node in the tree (root is ``0``).
    """

    id: str
    thought: str = ""
    evaluation_score: float = 0.0
    parent_id: str | None = None
    children_ids: list[str] = dc_field(default_factory=list)
    state: Any = None
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict."""
        return {
            "id": self.id,
            "thought": self.thought,
            "evaluation_score": self.evaluation_score,
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
            "state": self.state,
            "depth": self.depth,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThoughtNode:
        """Reconstruct a :class:`ThoughtNode` from a plain dict."""
        return cls(
            id=str(data.get("id", "")),
            thought=str(data.get("thought", "")),
            evaluation_score=float(data.get("evaluation_score", 0.0) or 0.0),
            parent_id=data.get("parent_id"),
            children_ids=[str(c) for c in data.get("children_ids", [])],
            state=data.get("state"),
            depth=int(data.get("depth", 0) or 0),
        )


class ThoughtTree:
    """An n-ary tree of :class:`ThoughtNode` objects.

    The root is created from an initial thought; children are attached via
    :meth:`add_child`. The tree supports path enumeration, best-path
    selection and score-threshold pruning.
    """

    def __init__(self, root_thought: str) -> None:
        """Create a tree whose root carries *root_thought* (score ``1.0``)."""
        self._counter = 0
        self.nodes: dict[str, ThoughtNode] = {}
        self.root_id = self._next_id()
        root = ThoughtNode(
            id=self.root_id,
            thought=root_thought,
            evaluation_score=1.0,
            parent_id=None,
            children_ids=[],
            state=None,
            depth=0,
        )
        self.nodes[self.root_id] = root

    def _next_id(self) -> str:
        """Return the next unique node id (``n1``, ``n2``, ...)."""
        self._counter += 1
        return f"n{self._counter}"

    @property
    def root(self) -> ThoughtNode:
        """The root node."""
        return self.nodes[self.root_id]

    def get_node(self, node_id: str) -> ThoughtNode | None:
        """Look up a node by id."""
        return self.nodes.get(node_id)

    def add_child(
        self,
        parent_id: str,
        thought: str,
        evaluation_score: float,
        state: Any = None,
    ) -> ThoughtNode | None:
        """Attach a child thought to *parent_id* and return the new node.

        Returns ``None`` when the parent is unknown.
        """
        parent = self.nodes.get(parent_id)
        if parent is None:
            logger.warning("add_child: parent %r not found", parent_id)
            return None
        child_id = self._next_id()
        child = ThoughtNode(
            id=child_id,
            thought=thought,
            evaluation_score=float(evaluation_score),
            parent_id=parent_id,
            children_ids=[],
            state=state,
            depth=parent.depth + 1,
        )
        self.nodes[child_id] = child
        parent.children_ids.append(child_id)
        return child

    def get_all_paths(self) -> list[list[ThoughtNode]]:
        """Return every root-to-leaf path as a list of nodes."""
        paths: list[list[ThoughtNode]] = []

        def _walk(node_id: str, current: list[ThoughtNode]) -> None:
            node = self.nodes.get(node_id)
            if node is None:
                return
            current = current + [node]
            # Children that still exist in the tree.
            live_children = [cid for cid in node.children_ids if cid in self.nodes]
            if not live_children:
                paths.append(current)
                return
            for cid in live_children:
                _walk(cid, current)

        _walk(self.root_id, [])
        return paths

    def get_best_path(self) -> list[ThoughtNode]:
        """Return the root-to-leaf path with the highest average score."""
        paths = self.get_all_paths()
        if not paths:
            return []

        def _avg(path: list[ThoughtNode]) -> float:
            scores = [n.evaluation_score for n in path]
            return sum(scores) / len(scores) if scores else 0.0

        return max(paths, key=_avg)

    def prune(self, threshold: float) -> int:
        """Remove nodes whose score is below *threshold* (and their subtrees).

        The root is never pruned. Returns the number of nodes removed.
        """
        # Identify seed nodes to prune (excluding root).
        seeds = {
            nid
            for nid, node in self.nodes.items()
            if nid != self.root_id and node.evaluation_score < threshold
        }

        # Expand seeds to full subtrees.
        removal: set[str] = set()

        def _collect_subtree(node_id: str, acc: set[str]) -> None:
            node = self.nodes.get(node_id)
            if node is None:
                return
            acc.add(node_id)
            for cid in node.children_ids:
                _collect_subtree(cid, acc)

        for seed in seeds:
            _collect_subtree(seed, removal)

        # Detach from parents.
        for nid in removal:
            node = self.nodes.get(nid)
            if node and node.parent_id:
                parent = self.nodes.get(node.parent_id)
                if parent and nid in parent.children_ids:
                    parent.children_ids.remove(nid)

        # Delete nodes.
        for nid in removal:
            self.nodes.pop(nid, None)

        if removal:
            logger.debug("Pruned %d nodes below threshold %.2f", len(removal), threshold)
        return len(removal)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the whole tree to a plain dict."""
        return {
            "root_id": self.root_id,
            "nodes": {nid: node.to_dict() for nid, node in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThoughtTree:
        """Reconstruct a :class:`ThoughtTree` from :meth:`to_dict` output."""
        tree = cls(root_thought="")  # placeholder root
        tree.nodes.clear()
        tree.root_id = str(data.get("root_id", ""))
        counter = 0
        for nid, ndata in (data.get("nodes") or {}).items():
            tree.nodes[nid] = ThoughtNode.from_dict(ndata)
            match = re.match(r"n(\d+)", nid)
            if match:
                counter = max(counter, int(match.group(1)))
        tree._counter = counter
        return tree


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_GENERATE_THOUGHTS_PROMPT = """你是一个深度推理专家，使用"思维树"方法探索问题的多种解法。

当前推理状态：
{state}

上一步思路：
{parent_thought}

请基于当前状态，生成 {n} 个不同的、有希望的下一步思路（每个思路应是一个具体的推理步骤或子问题的解法方向）。

请以 JSON 数组输出，每个元素是一个对象 {{"thought": "思路内容"}}。只输出 JSON，不要多余解释。"""

_EVALUATE_THOUGHT_PROMPT = """你是一个推理评估专家。请评估以下思路在解决当前问题中的前景。

当前推理状态：
{state}

待评估思路：
{thought}

请以 JSON 输出，字段：
- score: 0 到 1 之间的浮点数（1 表示极有希望，0 表示无价值/错误方向）
- reasoning: 简要评语

只输出 JSON。"""


# ---------------------------------------------------------------------------
# Tree-of-Thoughts driver
# ---------------------------------------------------------------------------


class TreeOfThoughts:
    """Drives a Tree-of-Thought search over a problem using an LLM.

    At each step the LLM proposes ``branching_factor`` candidate next
    thoughts, each is evaluated (0-1) in parallel, and the search proceeds
    breadth-first (beam) or depth-first. Nodes scoring below
    ``evaluation_threshold`` are pruned.

    Parameters
    ----------
    llm_provider:
        Any object exposing an async ``chat_completion(messages)`` and/or a
        synchronous ``chat_completion_sync(messages)`` method.
    max_depth:
        Maximum tree depth to explore (root is depth 0).
    branching_factor:
        Number of candidate thoughts generated at each node.
    evaluation_threshold:
        Minimum score for a thought to be retained (others are pruned).
    """

    def __init__(
        self,
        llm_provider: Any,
        max_depth: int = 3,
        branching_factor: int = 3,
        evaluation_threshold: float = 0.5,
    ) -> None:
        self.llm_provider = llm_provider
        self.max_depth = max(1, int(max_depth))
        self.branching_factor = max(1, int(branching_factor))
        self.evaluation_threshold = float(evaluation_threshold)
        # Results of the last search.
        self._tree: ThoughtTree | None = None
        self._best_path: list[ThoughtNode] = []

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------

    async def _call_llm_async(
        self, messages: list[dict[str, Any]], llm_provider: Any | None = None
    ) -> str:
        """Call the LLM, preferring the async API and falling back to a thread."""
        provider = llm_provider or self.llm_provider
        if provider is None:
            return ""
        chat_completion = getattr(provider, "chat_completion", None)
        if callable(chat_completion):
            try:
                result = await chat_completion(messages)
            except Exception as e:  # noqa: BLE001
                logger.debug("async chat_completion failed, falling back: %s", e)
                result = None
            if result is not None:
                if isinstance(result, str):
                    return result
                content = getattr(result, "content", None)
                if content:
                    return content
        sync_fn = getattr(provider, "chat_completion_sync", None)
        if callable(sync_fn):
            try:
                return await asyncio.to_thread(sync_fn, messages) or ""
            except Exception as e:  # noqa: BLE001
                logger.warning("LLM call failed: %s", e)
                return ""
        return ""

    def _format_state(self, state: Any) -> str:
        """Format a state payload into a compact textual representation for the LLM."""
        if state is None:
            return "(空状态)"
        if isinstance(state, str):
            return state
        if not isinstance(state, dict):
            try:
                return json.dumps(state, ensure_ascii=False)
            except (TypeError, ValueError):
                return str(state)

        lines: list[str] = []
        query = state.get("query")
        if query:
            lines.append(f"问题: {query}")
        context = state.get("context")
        if context:
            if isinstance(context, str):
                lines.append(f"上下文: {context}")
            else:
                try:
                    lines.append(f"上下文: {json.dumps(context, ensure_ascii=False)}")
                except (TypeError, ValueError):
                    lines.append(f"上下文: {context}")
        thoughts = state.get("thoughts", [])
        if thoughts:
            lines.append("已探索思路:")
            for i, t in enumerate(thoughts):
                lines.append(f"  {i + 1}. {t}")
        return "\n".join(lines) if lines else "(空状态)"

    @staticmethod
    def _advance_state(state: Any, thought: str) -> dict[str, Any]:
        """Return a new state with *thought* appended to the thought history."""
        if isinstance(state, dict):
            new_state = dict(state)
        else:
            new_state = {}
        thoughts = list(new_state.get("thoughts", []))
        thoughts.append(thought)
        new_state["thoughts"] = thoughts
        return new_state

    # ------------------------------------------------------------------
    # Thought generation & evaluation
    # ------------------------------------------------------------------

    async def generate_thoughts(
        self,
        state: Any,
        parent_thought: str,
        llm_provider: Any | None = None,
    ) -> list[str]:
        """Generate multiple candidate next thoughts for the current state.

        Returns up to ``branching_factor`` thought strings. Falls back to
        line-splitting the raw LLM output when JSON parsing fails.
        """
        provider = llm_provider or self.llm_provider
        if provider is None:
            logger.warning("generate_thoughts called without an LLM provider")
            return []
        prompt = _GENERATE_THOUGHTS_PROMPT.format(
            state=self._format_state(state),
            parent_thought=parent_thought or "(根节点)",
            n=self.branching_factor,
        )
        messages = [
            {"role": "system", "content": "你是深度推理专家，使用思维树方法，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        text = await self._call_llm_async(messages, provider)

        thoughts = self._parse_thoughts(text)
        if not thoughts and text:
            # Last resort: treat non-empty lines as thoughts.
            for line in text.strip().splitlines():
                cleaned = line.strip().lstrip("-*").strip()
                cleaned = re.sub(r"^\d+[.)]\s*", "", cleaned)
                if cleaned:
                    thoughts.append(cleaned)
        return thoughts[: self.branching_factor]

    @staticmethod
    def _parse_thoughts(text: str) -> list[str]:
        """Parse an LLM response into a list of thought strings."""
        data = _extract_json(text)
        thoughts: list[str] = []

        def _extract_from_item(item: Any) -> str:
            if isinstance(item, str):
                return item
            if isinstance(item, dict):
                for key in ("thought", "idea", "reasoning", "step"):
                    val = item.get(key)
                    if val:
                        return str(val)
            return ""

        if isinstance(data, list):
            for item in data:
                txt = _extract_from_item(item)
                if txt:
                    thoughts.append(txt)
        elif isinstance(data, dict):
            items = data.get("thoughts") or data.get("ideas") or data.get("steps")
            if isinstance(items, list):
                for item in items:
                    txt = _extract_from_item(item)
                    if txt:
                        thoughts.append(txt)
            elif isinstance(items, str):
                thoughts.append(items)
        return thoughts

    async def evaluate_thought(
        self,
        thought: str,
        state: Any,
        llm_provider: Any | None = None,
    ) -> float:
        """Evaluate a thought, returning a promise score in ``[0, 1]``."""
        provider = llm_provider or self.llm_provider
        if provider is None:
            return 0.5
        prompt = _EVALUATE_THOUGHT_PROMPT.format(
            state=self._format_state(state),
            thought=thought,
        )
        messages = [
            {"role": "system", "content": "你是推理评估专家，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        text = await self._call_llm_async(messages, provider)
        data = _extract_json(text)
        if isinstance(data, dict):
            score = data.get("score", data.get("evaluation", 0.5))
            try:
                return max(0.0, min(1.0, float(score)))
            except (TypeError, ValueError):
                pass
        return 0.5

    async def _evaluate_thoughts(self, thoughts: list[str], state: Any) -> list[float]:
        """Evaluate a batch of thoughts concurrently."""
        if not thoughts:
            return []
        tasks = [self.evaluate_thought(t, state, self.llm_provider) for t in thoughts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        scores: list[float] = []
        for r in results:
            if isinstance(r, Exception):
                logger.debug("evaluate_thought raised: %s", r)
                scores.append(0.0)
            elif isinstance(r, (int, float)):
                scores.append(max(0.0, min(1.0, float(r))))
            else:
                scores.append(0.0)
        return scores

    # ------------------------------------------------------------------
    # Search strategies
    # ------------------------------------------------------------------

    async def _expand_node(
        self, tree: ThoughtTree, node: ThoughtNode, state: Any
    ) -> list[ThoughtNode]:
        """Generate, evaluate and attach children for *node*."""
        thoughts = await self.generate_thoughts(state, node.thought, self.llm_provider)
        if not thoughts:
            return []
        scores = await self._evaluate_thoughts(thoughts, state)
        children: list[ThoughtNode] = []
        for thought, score in zip(thoughts, scores, strict=True):
            child_state = self._advance_state(state, thought)
            child = tree.add_child(node.id, thought, score, state=child_state)
            if child is not None:
                children.append(child)
        return children

    async def _bfs_search(
        self, tree: ThoughtTree, root: ThoughtNode, initial_state: dict[str, Any]
    ) -> None:
        """Breadth-first / beam search."""
        root.state = initial_state
        frontier: list[ThoughtNode] = [root]
        for _depth in range(1, self.max_depth + 1):
            if not frontier:
                break
            next_frontier: list[ThoughtNode] = []
            for node in frontier:
                state = node.state if node.state is not None else initial_state
                children = await self._expand_node(tree, node, state)
                # Keep children above the evaluation threshold.
                kept = [c for c in children if c.evaluation_score >= self.evaluation_threshold]
                if not kept and children:
                    # Keep the best child so the branch is not abandoned.
                    kept = [max(children, key=lambda c: c.evaluation_score)]
                next_frontier.extend(kept)
            # Beam: retain only the top ``branching_factor`` nodes.
            next_frontier.sort(key=lambda n: n.evaluation_score, reverse=True)
            frontier = next_frontier[: self.branching_factor]
        tree.prune(self.evaluation_threshold)

    async def _dfs_search(
        self,
        tree: ThoughtTree,
        node: ThoughtNode,
        state: Any,
        depth: int,
    ) -> None:
        """Depth-first, best-first descent."""
        if depth >= self.max_depth:
            return
        children = await self._expand_node(tree, node, state)
        if not children:
            return
        children.sort(key=lambda n: n.evaluation_score, reverse=True)
        explored = False
        for child in children[: self.branching_factor]:
            if child.evaluation_score >= self.evaluation_threshold:
                await self._dfs_search(tree, child, child.state, depth + 1)
                explored = True
        if not explored:
            # Make progress on the single best child even if below threshold.
            best = children[0]
            await self._dfs_search(tree, best, best.state, depth + 1)

    # ------------------------------------------------------------------
    # Public search API
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        context: Any = None,
        strategy: SearchStrategy | str = SearchStrategy.BFS,
    ) -> list[ThoughtNode]:
        """Run the ToT search and return the best root-to-leaf path.

        Parameters
        ----------
        query:
            The problem to solve.
        context:
            Optional extra context (string or dict) injected into the state.
        strategy:
            ``"bfs"`` (breadth-first beam) or ``"dfs"`` (depth-first).
        """
        strat = SearchStrategy(strategy) if not isinstance(strategy, SearchStrategy) else strategy
        initial_state: dict[str, Any] = {"query": query, "context": context, "thoughts": []}
        tree = ThoughtTree(root_thought=query)
        try:
            if strat == SearchStrategy.BFS:
                await self._bfs_search(tree, tree.root, initial_state)
            else:
                tree.root.state = initial_state
                await self._dfs_search(tree, tree.root, initial_state, depth=0)
        except Exception as e:  # noqa: BLE001
            logger.warning("ToT search raised: %s", e)
        self._tree = tree
        self._best_path = tree.get_best_path()
        return self._best_path

    def search_sync(
        self,
        query: str,
        context: Any = None,
        strategy: SearchStrategy | str = SearchStrategy.BFS,
    ) -> list[ThoughtNode]:
        """Synchronous wrapper for :meth:`search`."""
        return async_to_sync(self.search(query, context=context, strategy=strategy), timeout=180)

    def select_best_path(self) -> list[ThoughtNode]:
        """Return the best path found by the last :meth:`search` call."""
        return list(self._best_path)

    @property
    def tree(self) -> ThoughtTree | None:
        """The :class:`ThoughtTree` produced by the last search."""
        return self._tree
