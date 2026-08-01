"""Tool system for LLM agent - enables function calling and tool use.

This module implements the JSON Schema-based tool definition system that allows
LLMs to call external functions and APIs. Based on 2026 best practices:
- JSON Schema for tool definitions
- Parallel tool calls support
- Tool choice control (auto/none/specific)
- Error handling and retry logic
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from doctoragent._utils import async_to_sync

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool Definition
# ---------------------------------------------------------------------------


class ToolChoice(str, Enum):
    """Controls which tool the LLM should use."""

    AUTO = "auto"  # LLM decides whether to use a tool
    NONE = "none"  # Disable tool calling
    REQUIRED = "required"  # Force tool use


class ToolParameter(BaseModel):
    """Define a single parameter for a tool."""

    name: str
    type: str  # "string", "integer", "number", "boolean", "array", "object"
    description: str
    required: bool = True
    enum: list[str] | None = None
    default: Any = None


class ToolDefinition(BaseModel):
    """Complete definition of a tool that can be called by the LLM.

    Based on OpenAI/Anthropic function calling format (2026 standard).
    """

    name: str = Field(..., description="Unique tool name")
    description: str = Field(..., description="What the tool does")
    parameters: list[ToolParameter] = Field(default_factory=list)
    category: str = "general"

    def to_json_schema(self) -> dict[str, Any]:
        """Convert to JSON Schema format for LLM API."""
        properties = {}
        required = []

        for param in self.parameters:
            prop: dict[str, Any] = {"type": param.type, "description": param.description}
            if param.enum:
                prop["enum"] = param.enum
            if param.default is not None:
                prop["default"] = param.default
            properties[param.name] = prop
            if param.required:
                required.append(param.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """Format for OpenAI API tools parameter."""
        return [self.to_json_schema()]


class ToolResult(BaseModel):
    """Result from a tool execution."""

    success: bool
    data: Any = None
    error: str | None = None
    tool_name: str = ""
    execution_time_ms: float = 0


class Tool(ABC):
    """Base class for all tools."""

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Return the tool definition."""
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with given arguments."""
        ...

    def __call__(self, **kwargs: Any) -> ToolResult:
        """Synchronous wrapper for execute."""
        return async_to_sync(self.execute(**kwargs), timeout=30)


# ---------------------------------------------------------------------------
# Built-in Tools for DoctorAgent
# ---------------------------------------------------------------------------


class SearchDocumentsTool(Tool):
    """Search documents in the vault using RAG."""

    def __init__(self, rag_pipeline: Any = None):
        self.rag_pipeline = rag_pipeline

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_documents",
            description="Search for documents in the vault using natural language. Returns relevant document chunks with citations.",
            parameters=[
                ToolParameter(
                    name="query", type="string", description="Natural language search query"
                ),
                ToolParameter(
                    name="top_k",
                    type="integer",
                    description="Number of results to return",
                    required=False,
                    default=5,
                ),
                ToolParameter(
                    name="category",
                    type="string",
                    description="Filter by category (legal, finance, personal, other)",
                    required=False,
                ),
            ],
            category="retrieval",
        )

    async def execute(self, query: str, top_k: int = 5, category: str | None = None) -> ToolResult:
        """Execute document search."""
        import time

        start = time.time()

        try:
            if self.rag_pipeline is None:
                return ToolResult(success=False, error="RAG pipeline not initialized")

            response = self.rag_pipeline.ask(
                question=query,
                use_memory=False,
                use_query_expansion=True,
                top_k=top_k,
            )

            return ToolResult(
                success=True,
                data={
                    "answer": response.answer,
                    "sources": [
                        {
                            "file": r.chunk.get("vault_path", "unknown"),
                            "content": r.chunk.get("text", "")[:200],
                            "score": r.score,
                            "category": r.chunk.get("category", "unknown"),
                        }
                        for r in response.sources
                    ],
                    "total_chunks_searched": response.total_chunks_searched,
                },
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), tool_name=self.definition.name)


class ListFilesTool(Tool):
    """List files in the vault."""

    def __init__(self, task_store: Any = None):
        self.task_store = task_store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_files",
            description="List files in the encrypted vault. Shows file names, sizes, and categories.",
            parameters=[
                ToolParameter(
                    name="category", type="string", description="Filter by category", required=False
                ),
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="Max files to return",
                    required=False,
                    default=20,
                ),
            ],
            category="management",
        )

    async def execute(self, category: str | None = None, limit: int = 20) -> ToolResult:
        """List vault files."""
        import time

        start = time.time()

        try:
            if self.task_store is None:
                return ToolResult(success=False, error="Task store not initialized")

            with self.task_store._connect() as conn:
                query = "SELECT vault_path, category, created_at FROM files"
                params: list[Any] = []

                if category:
                    query += " WHERE category = ?"
                    params.append(category)

                query += " ORDER BY created_at DESC LIMIT ?"
                params.append(limit)

                rows = conn.execute(query, params).fetchall()

                files = [{"path": row[0], "category": row[1], "created_at": row[2]} for row in rows]

                return ToolResult(
                    success=True,
                    data={"files": files, "count": len(files)},
                    tool_name=self.definition.name,
                    execution_time_ms=(time.time() - start) * 1000,
                )
        except Exception as e:
            return ToolResult(success=False, error=str(e), tool_name=self.definition.name)


class GetFileDetailsTool(Tool):
    """Get detailed information about a specific file."""

    def __init__(self, task_store: Any = None):
        self.task_store = task_store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_file_details",
            description="Get detailed information about a specific file in the vault, including metadata and content preview.",
            parameters=[
                ToolParameter(
                    name="file_path", type="string", description="Path to the file in vault"
                ),
            ],
            category="management",
        )

    async def execute(self, file_path: str) -> ToolResult:
        """Get file details."""
        import time

        start = time.time()

        try:
            if self.task_store is None:
                return ToolResult(success=False, error="Task store not initialized")

            with self.task_store._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM files WHERE vault_path = ?", (file_path,)
                ).fetchone()

                if not row:
                    return ToolResult(success=False, error=f"File not found: {file_path}")

                return ToolResult(
                    success=True,
                    data={
                        "path": row[0],
                        "category": row[1],
                        "created_at": row[2],
                        "size": row[3] if len(row) > 3 else 0,
                    },
                    tool_name=self.definition.name,
                    execution_time_ms=(time.time() - start) * 1000,
                )
        except Exception as e:
            return ToolResult(success=False, error=str(e), tool_name=self.definition.name)


class AnalyzeDocumentTool(Tool):
    """Analyze document content using LLM."""

    def __init__(self, llm_provider: Any = None, rag_pipeline: Any = None):
        self.llm_provider = llm_provider
        self.rag_pipeline = rag_pipeline

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="analyze_document",
            description="Analyze a document's content. Can summarize, extract key information, or answer questions about the document.",
            parameters=[
                ToolParameter(
                    name="file_path", type="string", description="Path to the file in vault"
                ),
                ToolParameter(
                    name="analysis_type",
                    type="string",
                    description="Type of analysis",
                    enum=["summary", "key_points", "entities", "questions"],
                    default="summary",
                ),
                ToolParameter(
                    name="question",
                    type="string",
                    description="Specific question about the document",
                    required=False,
                ),
            ],
            category="analysis",
        )

    async def execute(
        self, file_path: str, analysis_type: str = "summary", question: str | None = None
    ) -> ToolResult:
        """Analyze document."""
        import time

        start = time.time()

        try:
            if self.rag_pipeline is None:
                return ToolResult(success=False, error="RAG pipeline not initialized")

            # Build analysis prompt
            prompts = {
                "summary": f"请总结文件 {file_path} 的主要内容",
                "key_points": f"请提取文件 {file_path} 的关键要点",
                "entities": f"请识别文件 {file_path} 中的实体（人名、日期、金额等）",
                "questions": f"请根据文件 {file_path} 回答：{question or '这个文件的主要内容是什么？'}",
            }

            query = prompts.get(analysis_type, prompts["summary"])

            response = self.rag_pipeline.ask(
                question=query,
                use_memory=False,
                use_query_expansion=False,
            )

            return ToolResult(
                success=True,
                data={
                    "file": file_path,
                    "analysis_type": analysis_type,
                    "result": response.answer,
                    "model_used": response.model_used,
                },
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), tool_name=self.definition.name)


class CompareDocumentsTool(Tool):
    """Compare multiple documents."""

    def __init__(self, rag_pipeline: Any = None):
        self.rag_pipeline = rag_pipeline

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="compare_documents",
            description="Compare multiple documents and identify similarities, differences, or conflicts.",
            parameters=[
                ToolParameter(
                    name="file_paths", type="array", description="List of file paths to compare"
                ),
                ToolParameter(
                    name="comparison_type",
                    type="string",
                    description="Type of comparison",
                    enum=["similarities", "differences", "conflicts", "timeline"],
                    default="differences",
                ),
            ],
            category="analysis",
        )

    async def execute(
        self, file_paths: list[str], comparison_type: str = "differences"
    ) -> ToolResult:
        """Compare documents."""
        import time

        start = time.time()

        try:
            if self.rag_pipeline is None:
                return ToolResult(success=False, error="RAG pipeline not initialized")

            files_str = ", ".join(file_paths)
            query = f"请比较以下文件的{comparison_type}：{files_str}"

            response = self.rag_pipeline.ask(
                question=query,
                use_memory=False,
                use_query_expansion=False,
            )

            return ToolResult(
                success=True,
                data={
                    "files": file_paths,
                    "comparison_type": comparison_type,
                    "result": response.answer,
                },
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), tool_name=self.definition.name)


class MemoryTool(Tool):
    """Store and recall memories."""

    def __init__(self, memory_system: Any = None):
        self.memory = memory_system

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="memory",
            description="Store or recall information from long-term memory. Use this to remember important facts about the user or their documents.",
            parameters=[
                ToolParameter(
                    name="action",
                    type="string",
                    description="Action to perform",
                    enum=["store", "recall", "list_facts"],
                ),
                ToolParameter(
                    name="content",
                    type="string",
                    description="Content to store or query for recall",
                    required=False,
                ),
                ToolParameter(
                    name="importance",
                    type="number",
                    description="Importance score (0-1)",
                    required=False,
                    default=0.7,
                ),
            ],
            category="memory",
        )

    async def execute(
        self, action: str, content: str | None = None, importance: float = 0.7
    ) -> ToolResult:
        """Execute memory action."""
        import time

        start = time.time()

        try:
            if self.memory is None:
                return ToolResult(success=False, error="Memory system not initialized")

            if action == "store" and content:
                self.memory.store_fact(content, importance=importance)
                return ToolResult(
                    success=True,
                    data={"action": "stored", "content": content},
                    tool_name=self.definition.name,
                    execution_time_ms=(time.time() - start) * 1000,
                )
            elif action == "recall" and content:
                facts = self.memory.recall_facts(content, limit=5)
                return ToolResult(
                    success=True,
                    data={
                        "action": "recalled",
                        "facts": [
                            {"content": f.content, "importance": f.importance} for f in facts
                        ],
                    },
                    tool_name=self.definition.name,
                    execution_time_ms=(time.time() - start) * 1000,
                )
            elif action == "list_facts":
                facts = self.memory.recall_facts("", limit=20)
                return ToolResult(
                    success=True,
                    data={
                        "action": "listed",
                        "facts": [
                            {"content": f.content, "importance": f.importance} for f in facts
                        ],
                    },
                    tool_name=self.definition.name,
                    execution_time_ms=(time.time() - start) * 1000,
                )
            else:
                return ToolResult(
                    success=False, error=f"Invalid action or missing content for {action}"
                )
        except Exception as e:
            return ToolResult(success=False, error=str(e), tool_name=self.definition.name)


class ExtractInformationTool(Tool):
    """Extract structured information from documents."""

    def __init__(self, rag_pipeline: Any = None):
        self.rag_pipeline = rag_pipeline

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="extract_information",
            description="Extract specific structured information from documents (dates, amounts, names, etc.).",
            parameters=[
                ToolParameter(name="file_path", type="string", description="File path in vault"),
                ToolParameter(
                    name="info_type",
                    type="string",
                    description="Type of information to extract",
                    enum=["dates", "amounts", "names", "addresses", "contracts", "all"],
                ),
            ],
            category="extraction",
        )

    async def execute(self, file_path: str, info_type: str = "all") -> ToolResult:
        """Extract information from document."""
        import time

        start = time.time()

        try:
            if self.rag_pipeline is None:
                return ToolResult(success=False, error="RAG pipeline not initialized")

            type_prompts = {
                "dates": "日期和时间信息",
                "amounts": "金额和数字信息",
                "names": "人名和组织名",
                "addresses": "地址信息",
                "contracts": "合同条款和条件",
                "all": "所有重要信息",
            }

            info_desc = type_prompts.get(info_type, "所有重要信息")
            query = f"请从文件 {file_path} 中提取{info_desc}，以JSON格式返回"

            response = self.rag_pipeline.ask(
                question=query,
                use_memory=False,
                use_query_expansion=False,
            )

            return ToolResult(
                success=True,
                data={
                    "file": file_path,
                    "info_type": info_type,
                    "extracted": response.answer,
                },
                tool_name=self.definition.name,
                execution_time_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), tool_name=self.definition.name)


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Registry for managing all available tools.

    In addition to registering and dispatching tools, the registry tracks
    per-tool failure counts and circuit-breaker state so the agent can skip a
    repeatedly failing tool for the rest of the session (see
    :meth:`record_failure`, :meth:`is_circuit_open`).

    Per-tool circuit-breaker configuration: call :meth:`configure_circuit`
    to override the global ``circuit_threshold`` / ``cooldown_seconds`` for
    a single tool (e.g. give an expensive remote API a lower threshold and
    a longer cooldown than a local in-process tool).
    """

    def __init__(self, circuit_threshold: int = 3, validate_args: bool = False) -> None:
        self._tools: dict[str, Tool] = {}
        # Per-tool failure bookkeeping for the circuit-breaker pattern.
        self._failure_counts: dict[str, int] = {}
        self._circuit_open: dict[str, bool] = {}
        self.circuit_threshold = circuit_threshold
        # 半开恢复：记录熔断打开时间戳，冷却后允许一次试探请求。
        self._circuit_opened_at: dict[str, float] = {}
        self._circuit_half_open: dict[str, bool] = {}
        self._circuit_cooldown_seconds = 60  # 熔断冷却时间（秒）
        # Per-tool overrides: tool_name -> {"threshold": int, "cooldown": float}
        self._circuit_overrides: dict[str, dict[str, Any]] = {}
        # Runtime arg validation against the declared ToolParameter schema.
        # Off by default for backward compatibility — many existing tools
        # advertise a loose schema (e.g. ``query``) but accept arbitrary
        # kwargs (``file_path`` …) and only fail inside ``execute``. Enable
        # explicitly when callers want strict LLM-arg checking.
        self.validate_args = validate_args

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.definition.name] = tool

    def configure_circuit(
        self,
        tool_name: str,
        threshold: int | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        """Override the circuit-breaker config for a single tool.

        Pass ``threshold=None`` / ``cooldown_seconds=None`` to keep the
        registry default for that field. Useful when one tool wraps a flaky
        remote API (low threshold, long cooldown) while another is a cheap
        in-process function (high threshold, short cooldown).
        """
        if tool_name not in self._tools:
            raise KeyError(f"Cannot configure unknown tool: {tool_name!r}")
        overrides = self._circuit_overrides.setdefault(tool_name, {})
        if threshold is not None:
            overrides["threshold"] = int(threshold)
        if cooldown_seconds is not None:
            overrides["cooldown"] = float(cooldown_seconds)

    def _effective_threshold(self, tool_name: str) -> int:
        overrides = self._circuit_overrides.get(tool_name, {})
        return int(overrides.get("threshold", self.circuit_threshold))

    def _effective_cooldown(self, tool_name: str) -> float:
        overrides = self._circuit_overrides.get(tool_name, {})
        return float(overrides.get("cooldown", self._circuit_cooldown_seconds))

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        """List all registered tool definitions."""
        return [tool.definition for tool in self._tools.values()]

    def list_by_category(self, category: str) -> list[ToolDefinition]:
        """List tools by category."""
        return [
            tool.definition for tool in self._tools.values() if tool.definition.category == category
        ]

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """Convert all tools to OpenAI API format."""
        tools = []
        for tool in self._tools.values():
            tools.extend(tool.definition.to_openai_tools())
        return tools

    async def execute(self, tool_name: str, **kwargs: Any) -> ToolResult:
        """Execute a tool by name.

        When :attr:`validate_args` is ``True``, performs runtime type
        validation against the tool's declared ``ToolParameter`` schema
        *before* dispatching: missing required params and obviously-wrong
        types (``string`` param given an int, ``array`` param given a
        str, …) return a structured :class:`ToolResult` failure instead
        of crashing inside the tool. The validation is intentionally
        lenient — it catches the mistakes LLMs commonly make
        (stringifying numbers, sending ``null`` for a required param)
        without rejecting valid coerce-able values.
        """
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"Tool not found: {tool_name}")

        # ── Runtime type validation (opt-in) ──────────────────────────
        if self.validate_args:
            validation_error = self._validate_args(tool, kwargs)
            if validation_error is not None:
                return ToolResult(
                    success=False,
                    error=validation_error,
                    tool_name=tool_name,
                )

        return await tool.execute(**kwargs)

    @staticmethod
    def _validate_args(tool: Tool, args: dict[str, Any]) -> str | None:
        """Validate *args* against *tool*'s parameter schema.

        Returns ``None`` when valid, or a human-readable error string. The
        checks mirror what an LLM is most likely to get wrong:

        * missing required parameter
        * ``None`` for a required parameter
        * gross type mismatch (declared ``integer`` but got ``list`` …)
        * enum value not in the declared enum set

        Coercions the tool itself can handle (e.g. ``"42"`` for an
        ``integer`` param) are *not* rejected here — the tool's own
        ``execute`` is the authority on what it accepts.
        """
        declared = {p.name: p for p in tool.definition.parameters}
        # Reject unknown parameters only when the tool declared at least
        # one parameter (an empty schema means "accept anything").
        for name, value in args.items():
            param = declared.get(name)
            if param is None:
                continue  # tolerate extras — tools may accept **kwargs
            if value is None:
                if param.required:
                    return f"Parameter '{name}' is required but got None"
                continue
            type_mismatch = ToolRegistry._check_type(param.type, value)
            if type_mismatch is not None:
                return f"Parameter '{name}' declared as '{param.type}' but received {type_mismatch}"
            if param.enum is not None and str(value) not in param.enum:
                return f"Parameter '{name}' value '{value}' is not in allowed enum {param.enum}"
        # Required-presence check (covers params absent from kwargs entirely).
        for name, param in declared.items():
            if param.required and name not in args:
                return f"Missing required parameter: '{name}'"
        return None

    @staticmethod
    def _check_type(declared_type: str, value: Any) -> str | None:
        """Return a mismatch description or ``None`` when the type fits.

        Only flags *gross* mismatches (e.g. ``array`` declared but a str
        received). Numbers are tolerated across int/float; booleans are
        NOT accepted where a string/int is declared (LLMs often send
        ``true``/``false`` for flags that expect ``"yes"``/``"no"``).
        """
        if declared_type == "string":
            if isinstance(value, bool):
                return "bool (expected str)"
            if not isinstance(value, str):
                return f"{type(value).__name__} (expected str)"
        elif declared_type in ("integer", "number"):
            if isinstance(value, bool):
                return "bool (expected number)"
            if not isinstance(value, (int, float)):
                return f"{type(value).__name__} (expected {declared_type})"
        elif declared_type == "boolean":
            if not isinstance(value, bool):
                return f"{type(value).__name__} (expected bool)"
        elif declared_type == "array":
            if not isinstance(value, list):
                return f"{type(value).__name__} (expected array)"
        elif declared_type == "object":
            if not isinstance(value, dict):
                return f"{type(value).__name__} (expected object)"
        return None

    def execute_sync(self, tool_name: str, **kwargs: Any) -> ToolResult:
        """Synchronous wrapper for execute."""
        return async_to_sync(self.execute(tool_name, **kwargs), timeout=60)

    # ------------------------------------------------------------------
    # Circuit-breaker state (error recovery layer)
    # ------------------------------------------------------------------

    def record_failure(self, tool_name: str) -> int:
        """Record a failed execution for *tool_name*.

        Returns the updated failure count. When the count reaches the
        per-tool threshold (or the registry default when no override is
        set) the tool's circuit is opened (with a timestamp) and the tool
        will be skipped by :meth:`is_circuit_open` until the cooldown
        elapses (auto half-open recovery) or :meth:`reset_circuit` is
        called.
        """
        count = self._failure_counts.get(tool_name, 0) + 1
        self._failure_counts[tool_name] = count
        threshold = self._effective_threshold(tool_name)
        if count >= threshold:
            self._circuit_open[tool_name] = True
            self._circuit_opened_at[tool_name] = time.time()
            self._circuit_half_open[tool_name] = False
            logger.warning(
                "Circuit breaker opened for tool %r after %d failures (threshold=%d)",
                tool_name,
                count,
                threshold,
            )
        return count

    def record_success(self, tool_name: str) -> None:
        """Record a successful execution, resetting the failure counter.

        Clears the open / half-open state and the opened-at timestamp so
        the breaker is fully closed after a successful call (including a
        half-open trial probe).
        """
        self._failure_counts[tool_name] = 0
        self._circuit_open[tool_name] = False
        self._circuit_half_open[tool_name] = False
        self._circuit_opened_at.pop(tool_name, None)

    def is_circuit_open(self, tool_name: str) -> bool:
        """Whether the tool is currently tripped (skipped) by the breaker.

        Implements automatic half-open recovery: once the per-tool cooldown
        has elapsed since the circuit opened, the breaker transitions to
        half-open and allows a single trial request through (returns
        ``False``). A subsequent success closes the breaker via
        :meth:`record_success`; a failure re-opens it via
        :meth:`record_failure`.
        """
        if not self._circuit_open.get(tool_name, False):
            return False
        # 检查是否到了半开恢复时间
        opened_at = self._circuit_opened_at.get(tool_name, 0)
        cooldown = self._effective_cooldown(tool_name)
        if time.time() - opened_at > cooldown:
            # 半开：允许一次试探请求
            self._circuit_open[tool_name] = False
            self._circuit_half_open[tool_name] = True
            logger.info("工具 %s 熔断器进入半开状态 (cooldown=%.0fs)", tool_name, cooldown)
            return False
        return True

    def reset_circuit(self, tool_name: str) -> None:
        """Manually reset a tool's circuit breaker and failure count."""
        self._failure_counts[tool_name] = 0
        self._circuit_open[tool_name] = False
        self._circuit_half_open[tool_name] = False
        self._circuit_opened_at.pop(tool_name, None)

    def get_failure_count(self, tool_name: str) -> int:
        """Return the current consecutive failure count for a tool."""
        return self._failure_counts.get(tool_name, 0)

    def get_alternative_tool(self, tool_name: str) -> str | None:
        """Find a healthy alternative tool in the same category.

        Returns the name of the first non-tripped tool that shares the
        category of *tool_name* (excluding *tool_name* itself), or ``None``
        when no alternative exists. Used by the agent's error-recovery layer
        when a tool fails permanently or trips its circuit breaker.
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return None
        category = tool.definition.category
        for candidate_name, candidate in self._tools.items():
            if candidate_name == tool_name:
                continue
            if candidate.definition.category != category:
                continue
            if not self.is_circuit_open(candidate_name):
                return candidate_name
        return None

    def compatible_args(self, tool_name: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Filter *args* to *tool_name*'s declared parameter signature.

        The error-recovery layer forwards the original tool call's arguments
        verbatim to a same-category alternative tool. Same-category clinical
        tools are NOT signature-compatible (e.g. ``check_vitals`` takes
        ``vitals`` while its alternative ``check_drug_interactions`` takes
        ``drugs``), so forwarding verbatim raised
        ``TypeError: ... got an unexpected keyword argument`` on every
        fallback. This helper drops args not declared on the target tool and
        reports whether the remaining args satisfy the target's *required*
        parameters — letting the caller skip a doomed substitute call instead
        of crashing into the generic ``except Exception``.

        Returns ``(filtered_args, satisfied)`` where ``satisfied`` is
        ``False`` when the tool is unknown or a required parameter is
        missing from *args*.
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            return {}, False
        declared = {p.name: p for p in tool.definition.parameters}
        filtered = {k: v for k, v in args.items() if k in declared}
        for name, param in declared.items():
            if param.required and name not in filtered:
                return filtered, False
        return filtered, True


def create_default_registry(
    rag_pipeline: Any = None,
    task_store: Any = None,
    memory_system: Any = None,
    llm_provider: Any = None,
) -> ToolRegistry:
    """Create a tool registry with all default tools."""
    registry = ToolRegistry()

    # Register tools
    registry.register(SearchDocumentsTool(rag_pipeline))
    registry.register(ListFilesTool(task_store))
    registry.register(GetFileDetailsTool(task_store))
    registry.register(AnalyzeDocumentTool(llm_provider, rag_pipeline))
    registry.register(CompareDocumentsTool(rag_pipeline))
    registry.register(MemoryTool(memory_system))
    registry.register(ExtractInformationTool(rag_pipeline))

    return registry
