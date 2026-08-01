"""Dynamic tool creation framework - allows agents to compose new tools from existing ones.

Implements:
- Tool composition: combine multiple tools into a new composite tool
- Dynamic tool generation: LLM generates tool definitions from natural language descriptions
- Tool template system: reusable templates for common tool patterns
- Runtime tool registration: dynamically add/remove tools at runtime

The :class:`DynamicToolFactory` collaborates with a :class:`~doctoragent.model.tools.ToolRegistry`.
Generated :class:`CompositeTool` instances chain existing tools together: the output of each
step feeds the next according to per-step *input mappings* (source expressions).

Source-expression grammar (used in ``input_mapping`` values)::

    "input:<name>"          -> external kwarg <name> passed to the composite tool
    "step:<n>"              -> full data output of step <n> (0-indexed)
    "step:<n>:<key>"        -> <key> within step <n>'s data dict
    "output:<key>"          -> a named output (output_key) produced by an earlier step
    <any other value>       -> treated as a literal constant
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from doctoragent.compat import StrEnum
from doctoragent.model.agent import _extract_json

from .tools import Tool, ToolDefinition, ToolParameter, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ToolCategory(StrEnum):
    """Category labels for dynamically created tools."""

    DYNAMIC = "dynamic"
    COMPOSITE = "composite"
    PIPELINE = "pipeline"


@dataclass
class ToolTemplate:
    """Reusable template describing how to build a tool.

    Templates capture the *shape* of a tool (name, description, parameter
    schema, the implementation template and the existing tools it depends on)
    so it can be materialised later into a concrete :class:`CompositeTool`.
    """

    name: str
    description: str
    parameter_schema: dict[str, Any] = dc_field(default_factory=dict)
    implementation_template: str = ""
    required_tools: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict."""
        return {
            "name": self.name,
            "description": self.description,
            "parameter_schema": self.parameter_schema,
            "implementation_template": self.implementation_template,
            "required_tools": self.required_tools,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolTemplate:
        """Build a :class:`ToolTemplate` from a parsed dict."""
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            parameter_schema=dict(data.get("parameter_schema", {}) or {}),
            implementation_template=str(data.get("implementation_template", "")),
            required_tools=[str(t) for t in data.get("required_tools", []) if t],
        )


@dataclass
class ToolChainStep:
    """A single step in a :class:`ToolChain`.

    ``input_mapping`` maps a *chain input source* (key) to the target *tool
    parameter name* (value). Source expressions follow the grammar documented
    in the module docstring (e.g. ``"input:query"``, ``"output:summary"``).
    ``output_key`` optionally names this step's output so later steps can
    reference it via ``"output:<output_key>"``.
    """

    tool_name: str
    input_mapping: dict[str, str] = dc_field(default_factory=dict)
    output_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict."""
        return {
            "tool_name": self.tool_name,
            "input_mapping": dict(self.input_mapping),
            "output_key": self.output_key,
        }


@dataclass
class ToolChain:
    """A named, ordered sequence of :class:`ToolChainStep` objects."""

    name: str
    steps: list[ToolChainStep] = dc_field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict."""
        return {
            "name": self.name,
            "description": self.description,
            "steps": [s.to_dict() for s in self.steps],
        }


# ---------------------------------------------------------------------------
# Composite tool
# ---------------------------------------------------------------------------


class CompositeTool(Tool):
    """A tool that chains multiple existing tools together.

    Parameters
    ----------
    name:
        Unique name for the composite tool.
    description:
        Human-readable description of what the composite does.
    tool_chain:
        Ordered list of ``(tool_name, input_mapping)`` tuples (an optional
        third element ``output_key`` may name the step's output). Each
        ``input_mapping`` maps a *tool parameter name* to a *source
        expression* (see the module docstring).
    registry:
        The :class:`~doctoragent.model.tools.ToolRegistry` holding the underlying
        tools that will be executed.
    """

    def __init__(
        self,
        name: str,
        description: str,
        tool_chain: list[Any],
        registry: ToolRegistry,
    ) -> None:
        self._name = name
        self._description = description
        # Normalise entries to (tool_name, mapping, output_key) triples.
        self.tool_chain: list[tuple[str, dict[str, str], str | None]] = []
        for entry in tool_chain:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                logger.warning("Skipping malformed tool_chain entry: %r", entry)
                continue
            tool_name = str(entry[0])
            mapping = dict(entry[1] or {})
            output_key = entry[2] if len(entry) > 2 else None
            self.tool_chain.append((tool_name, mapping, output_key))
        self.registry = registry

    # ------------------------------------------------------------------
    # Tool interface
    # ------------------------------------------------------------------

    @property
    def definition(self) -> ToolDefinition:
        """The :class:`ToolDefinition` exposed to the LLM / registry."""
        return self.to_tool_definition()

    def to_tool_definition(self) -> ToolDefinition:
        """Convert to a :class:`ToolDefinition` compatible with ``ToolRegistry``."""
        params = self._derive_parameters()
        return ToolDefinition(
            name=self._name,
            description=self._description,
            parameters=params,
            category=ToolCategory.COMPOSITE.value,
        )

    def _derive_parameters(self) -> list[ToolParameter]:
        """Derive external parameters from ``input:`` source references."""
        names: list[str] = []
        for _tool_name, mapping, _output_key in self.tool_chain:
            for source in mapping.values():
                if isinstance(source, str) and source.startswith("input:"):
                    ext = source.split(":", 1)[1]
                    if ext and ext not in names:
                        names.append(ext)
        return [
            ToolParameter(
                name=n,
                type="string",
                description=f"Input parameter '{n}' for composite tool '{self._name}'.",
                required=True,
            )
            for n in names
        ]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the chain: pass the output of each tool as input to the next.

        Execution stops at the first failing step. The final step's result is
        returned (re-tagged with this composite tool's name).
        """
        start = time.time()
        context: dict[str, Any] = {"input": dict(kwargs)}
        last_result = ToolResult(success=True, data=None, tool_name=self._name)

        if not self.tool_chain:
            return ToolResult(
                success=False,
                error=f"Composite tool '{self._name}' has an empty tool chain.",
                tool_name=self._name,
                execution_time_ms=(time.time() - start) * 1000,
            )

        for i, (tool_name, mapping, output_key) in enumerate(self.tool_chain):
            args = self._resolve_args(mapping, context)
            try:
                result = await self.registry.execute(tool_name, **args)
            except Exception as e:  # noqa: BLE001
                logger.warning("Composite step %d (%s) raised: %s", i, tool_name, e)
                return ToolResult(
                    success=False,
                    error=f"Step {i} ({tool_name}) raised: {e}",
                    tool_name=self._name,
                    execution_time_ms=(time.time() - start) * 1000,
                )

            data = result.data if result.success else None
            context[f"step{i}"] = data
            if output_key:
                context[f"output:{output_key}"] = data
            last_result = result

            if not result.success:
                logger.warning(
                    "Composite tool %s stopped at step %d (%s): %s",
                    self._name,
                    i,
                    tool_name,
                    result.error,
                )
                return ToolResult(
                    success=False,
                    error=f"Step {i} ({tool_name}) failed: {result.error}",
                    data=result.data,
                    tool_name=self._name,
                    execution_time_ms=(time.time() - start) * 1000,
                )

        return ToolResult(
            success=last_result.success,
            data=last_result.data,
            error=last_result.error,
            tool_name=self._name,
            execution_time_ms=(time.time() - start) * 1000,
        )

    def _resolve_args(self, mapping: dict[str, str], context: dict[str, Any]) -> dict[str, Any]:
        """Resolve a param->source mapping into concrete argument values."""
        args: dict[str, Any] = {}
        for param, source in mapping.items():
            args[param] = self._resolve_source(source, context)
        return args

    @staticmethod
    def _resolve_source(source: Any, context: dict[str, Any]) -> Any:
        """Resolve a source expression against the execution context."""
        if not isinstance(source, str):
            return source
        if source.startswith("input:"):
            key = source.split(":", 1)[1]
            return context.get("input", {}).get(key, source)
        if source.startswith("step:"):
            rest = source.split(":", 1)[1]
            parts = rest.split(":", 1)
            step_key = f"step{parts[0]}"
            value = context.get(step_key)
            if len(parts) > 1 and isinstance(value, dict):
                return value.get(parts[1], source)
            return value
        if source.startswith("output:"):
            key = source.split(":", 1)[1]
            return context.get(f"output:{key}", source)
        return source

    def to_dict(self) -> dict[str, Any]:
        """Serialise the composite tool to a plain dict."""
        return {
            "name": self._name,
            "description": self._description,
            "tool_chain": [
                [tool_name, mapping, output_key]
                for tool_name, mapping, output_key in self.tool_chain
            ],
        }


# ---------------------------------------------------------------------------
# Dynamic tool factory
# ---------------------------------------------------------------------------

_TOOL_GENERATION_PROMPT = """你是一个动态工具生成专家。请根据自然语言描述，利用已有的工具组合出一个新工具。

新工具描述：{description}

已有工具列表：
{available_tools}

请输出 JSON，字段如下：
- name: 新工具的唯一名称（英文蛇形命名，如 search_and_summarize）
- description: 新工具的功能描述
- parameters: 数组，每个元素 {{"name": ..., "type": "string|integer|number|boolean|array|object", "description": ..., "required": true|false}}
- required_tools: 字符串数组，该工具依赖的已有工具名
- tool_chain: 数组，描述执行链。每个元素 {{"tool": 工具名, "input_mapping": {{参数名: 来源}}, "output_key": 可选输出键名}}
  其中"来源"使用以下格式：
    "input:<外部参数名>" 表示取外部传入参数
    "step:<n>" 表示取第 n 步（从0开始）的输出
    "output:<输出键名>" 表示取之前某步的命名输出
    其他字符串视为字面量

只输出 JSON，不要多余解释。"""

_COMPOSE_TOOLS_PROMPT = """你是工具组合专家。请将以下已有工具组合成一个新工具以满足描述。

组合目标描述：{composition_description}

待组合的工具：{tool_names}

已有工具详情：
{available_tools}

请输出 JSON，字段：
- name: 新工具名
- description: 功能描述
- tool_chain: 数组，每个元素 {{"tool": 工具名, "input_mapping": {{参数名: 来源}}, "output_key": 可选}}
  来源格式同上（input:/step:/output: 前缀）。

只输出 JSON。"""


class DynamicToolFactory:
    """Factory that creates, composes and manages dynamic tools at runtime.

    Wraps a :class:`~doctoragent.model.tools.ToolRegistry` and an LLM provider.
    Dynamically created tools are tracked separately so they can be listed
    and removed without affecting the registry's static tools.
    """

    def __init__(self, llm_provider: Any, registry: ToolRegistry) -> None:
        """Initialise the factory.

        Parameters
        ----------
        llm_provider:
            Any object exposing ``chat_completion_sync(messages) -> str``.
        registry:
            The tool registry that holds the underlying (static) tools and
            where dynamic tools will be registered.
        """
        self.llm_provider = llm_provider
        self.registry = registry
        # name -> Tool, tracking only dynamically created tools.
        self._dynamic_tools: dict[str, Tool] = {}

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    def _available_tools_description(self) -> str:
        """Render the registry's tools as a compact text description."""
        try:
            tools = self.registry.list_tools()
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to list registry tools: %s", e)
            return "(无法获取工具列表)"
        if not tools:
            return "(无可用工具)"
        lines = []
        for t in tools:
            params = ", ".join(f"{p.name}({p.type})" for p in t.parameters) or "(无参数)"
            lines.append(f"- {t.name}: {t.description} | 参数: {params}")
        return "\n".join(lines)

    @staticmethod
    def _normalize_input_mapping(raw: Any) -> dict[str, str]:
        """Normalise an LLM-provided input mapping to ``{param: source}``."""
        if not isinstance(raw, dict):
            return {}
        mapping: dict[str, str] = {}
        for key, value in raw.items():
            # Tolerate either direction; coerce values to strings.
            mapping[str(key)] = (
                str(value)
                if not isinstance(value, (dict, list))
                else json.dumps(value, ensure_ascii=False)
            )
        return mapping

    def _build_chain_from_json(
        self, raw_chain: Any
    ) -> list[tuple[str, dict[str, str], str | None]]:
        """Convert an LLM tool_chain JSON array into CompositeTool tuples."""
        chain: list[tuple[str, dict[str, str], str | None]] = []
        if not isinstance(raw_chain, list):
            return chain
        for item in raw_chain:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool") or item.get("tool_name") or "")
            if not tool_name:
                continue
            mapping = self._normalize_input_mapping(item.get("input_mapping", {}))
            output_key = item.get("output_key")
            chain.append((tool_name, mapping, output_key if output_key else None))
        return chain

    # ------------------------------------------------------------------
    # Creation API
    # ------------------------------------------------------------------

    def create_tool_from_description(
        self, description: str, llm_provider: Any | None = None
    ) -> Tool | ToolTemplate | None:
        """Generate a new tool definition from a natural-language *description*.

        The LLM is asked to design a tool that composes existing registry
        tools. When the design includes a ``tool_chain``, a registered
        :class:`CompositeTool` is returned; otherwise a :class:`ToolTemplate`
        describing the desired tool is returned. Returns ``None`` on failure.
        """
        provider = llm_provider or self.llm_provider
        if provider is None:
            logger.warning("create_tool_from_description called without an LLM provider")
            return None
        prompt = _TOOL_GENERATION_PROMPT.format(
            description=description,
            available_tools=self._available_tools_description(),
        )
        messages = [
            {"role": "system", "content": "你是动态工具生成专家，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        try:
            text = provider.chat_completion_sync(messages) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Tool generation LLM call failed: %s", e)
            return None

        data = _extract_json(text)
        if not isinstance(data, dict):
            logger.warning("Tool generation produced no parseable JSON.")
            return None

        name = str(data.get("name", "")).strip()
        desc = str(data.get("description", "")).strip()
        if not name:
            logger.warning("Generated tool has no name; rejecting.")
            return None

        raw_chain = data.get("tool_chain")
        chain = self._build_chain_from_json(raw_chain)
        if chain:
            tool = CompositeTool(
                name=name,
                description=desc or description,
                tool_chain=chain,
                registry=self.registry,
            )
            if self.validate_tool_definition(tool.to_tool_definition()):
                return tool
            logger.warning("Generated composite tool %s failed validation.", name)
            return tool  # still return; caller decides whether to register

        # No executable chain -> return a template describing the desired tool.
        params_schema = data.get("parameters", {})
        if isinstance(params_schema, list):
            params_schema = {
                p.get("name", str(i)): p for i, p in enumerate(params_schema) if isinstance(p, dict)
            }
        return ToolTemplate(
            name=name,
            description=desc or description,
            parameter_schema=dict(params_schema or {}),
            implementation_template=str(data.get("implementation_template", "")),
            required_tools=[str(t) for t in data.get("required_tools", []) if t],
        )

    def compose_tools(
        self,
        tool_names: list[str],
        composition_description: str,
        llm_provider: Any | None = None,
    ) -> CompositeTool | None:
        """Compose multiple existing tools into a single :class:`CompositeTool`.

        Parameters
        ----------
        tool_names:
            Names of the existing registry tools to combine.
        composition_description:
            Natural-language description of the desired composition.
        llm_provider:
            Optional override LLM provider.
        """
        provider = llm_provider or self.llm_provider
        if provider is None:
            logger.warning("compose_tools called without an LLM provider")
            return None
        # Filter to tools that actually exist in the registry.
        existing = []
        for name in tool_names:
            if self.registry.get(name) is not None:
                existing.append(name)
            else:
                logger.warning("compose_tools: tool %r not in registry; skipping.", name)
        if not existing:
            logger.warning("compose_tools: no valid tools to compose.")
            return None

        prompt = _COMPOSE_TOOLS_PROMPT.format(
            composition_description=composition_description,
            tool_names=", ".join(existing),
            available_tools=self._available_tools_description(),
        )
        messages = [
            {"role": "system", "content": "你是工具组合专家，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]
        try:
            text = provider.chat_completion_sync(messages) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Tool composition LLM call failed: %s", e)
            return None

        data = _extract_json(text)
        if not isinstance(data, dict):
            logger.warning("Tool composition produced no parseable JSON.")
            return None

        name = str(data.get("name", "")).strip() or "_".join(existing) + "_composed"
        desc = str(data.get("description", "")).strip() or composition_description
        chain = self._build_chain_from_json(data.get("tool_chain"))
        if not chain:
            logger.warning("Composed tool %s has an empty chain.", name)
            return None
        tool = CompositeTool(
            name=name,
            description=desc,
            tool_chain=chain,
            registry=self.registry,
        )
        return tool

    def create_pipeline_tool(
        self,
        steps: list[ToolChainStep],
        name: str,
        description: str,
    ) -> CompositeTool:
        """Create a pipeline (composite) tool from a list of :class:`ToolChainStep`.

        Each step's ``input_mapping`` (chain input -> tool param) is inverted
        to the param->source convention used by :class:`CompositeTool`, and
        each step's ``output_key`` is preserved so later steps can reference
        named outputs via ``"output:<key>"``.
        """
        chain: list[tuple[str, dict[str, str], str | None]] = []
        for step in steps:
            # Invert {source: param} -> {param: source}.
            mapping = {param: source for source, param in step.input_mapping.items()}
            chain.append((step.tool_name, mapping, step.output_key))
        return CompositeTool(
            name=name,
            description=description,
            tool_chain=chain,
            registry=self.registry,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_tool_definition(definition: ToolDefinition | dict[str, Any]) -> bool:
        """Validate a generated tool definition.

        Accepts a :class:`ToolDefinition` or a plain dict. Returns ``True``
        when the definition has a non-empty name, a non-empty description and
        a well-formed parameter list.
        """
        if isinstance(definition, ToolDefinition):
            name = definition.name
            desc = definition.description
            params = definition.parameters
        elif isinstance(definition, dict):
            name = definition.get("name", "")
            desc = definition.get("description", "")
            params = definition.get("parameters", [])
        else:
            logger.warning("validate_tool_definition: unsupported type %r", type(definition))
            return False

        if not name or not str(name).strip():
            logger.warning("validate_tool_definition: missing name.")
            return False
        if not desc or not str(desc).strip():
            logger.warning("validate_tool_definition: missing description.")
            return False
        if not isinstance(params, list):
            logger.warning("validate_tool_definition: parameters is not a list.")
            return False
        valid_types = {"string", "integer", "number", "boolean", "array", "object"}
        for i, p in enumerate(params):
            if isinstance(p, ToolParameter):
                pname, ptype = p.name, p.type
            elif isinstance(p, dict):
                pname, ptype = p.get("name", ""), p.get("type", "")
            else:
                logger.warning("validate_tool_definition: parameter %d malformed.", i)
                return False
            if not pname:
                logger.warning("validate_tool_definition: parameter %d missing name.", i)
                return False
            if ptype and ptype not in valid_types:
                logger.warning(
                    "validate_tool_definition: parameter %d has invalid type %r.", i, ptype
                )
                return False
        return True

    # ------------------------------------------------------------------
    # Runtime registration
    # ------------------------------------------------------------------

    def register_dynamic_tool(self, tool: Tool) -> bool:
        """Register a dynamically created *tool* in the registry.

        Returns ``True`` on success. The tool is tracked so it can later be
        removed via :meth:`unregister_dynamic_tool`.
        """
        try:
            name = tool.definition.name
        except Exception as e:  # noqa: BLE001
            logger.error("Cannot read tool definition name: %s", e)
            return False
        try:
            self.registry.register(tool)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to register dynamic tool %r: %s", name, e)
            return False
        self._dynamic_tools[name] = tool
        logger.debug("Registered dynamic tool %r", name)
        return True

    def unregister_dynamic_tool(self, tool_name: str) -> bool:
        """Remove a dynamically created tool from the registry.

        Only tools tracked by this factory are removed (static registry tools
        are never touched). Returns ``True`` if a tool was removed.
        """
        if tool_name not in self._dynamic_tools:
            logger.warning("unregister_dynamic_tool: %r is not a dynamic tool.", tool_name)
            return False
        removed = self._dynamic_tools.pop(tool_name, None)
        # Best-effort removal from the registry's internal store.
        tools_dict = getattr(self.registry, "_tools", None)
        if isinstance(tools_dict, dict):
            tools_dict.pop(tool_name, None)
        # Clean up circuit-breaker bookkeeping if present.
        for attr in ("_failure_counts", "_circuit_open"):
            book = getattr(self.registry, attr, None)
            if isinstance(book, dict):
                book.pop(tool_name, None)
        logger.debug("Unregistered dynamic tool %r (existed=%s)", tool_name, removed is not None)
        return removed is not None

    def list_dynamic_tools(self) -> list[ToolDefinition]:
        """List definitions of all dynamically created tools."""
        definitions: list[ToolDefinition] = []
        for tool in self._dynamic_tools.values():
            try:
                definitions.append(tool.definition)
            except Exception as e:  # noqa: BLE001
                logger.debug("Skipping dynamic tool with unreadable definition: %s", e)
        return definitions
