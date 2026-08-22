"""General-purpose agent tools (what every general agent should have).

Fills the classic gaps: live web search, web page fetch, current time, and safe
math calculation. These are the "table stakes" tools of a general assistant that
the clinical/domain tools do not provide.

* ``web_search`` — live search (pluggable backend; defaults to DuckDuckGo HTML,
  override with ``DOCTORAGENT_SEARCH_URL``. Accepts either the native JSON
  shape ``{results:[{title, url, snippet}]}`` or a SearXNG instance with
  ``format=json`` enabled — SearXNG's ``content`` field is normalized to
  ``snippet`` automatically).
* ``web_fetch`` — fetch a URL and extract readable text.
* ``current_time`` — current date/time + timezone.
* ``calculate`` — safe arithmetic evaluation (no eval of arbitrary code).

Register via :func:`register_general_tools`.
"""

from __future__ import annotations

import datetime
import logging
import re
import urllib.parse
from typing import Any

import httpx

from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class _GTool(Tool):
    name = ""
    description = ""
    category = "general"
    parameters: list[dict[str, Any]] = []

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=[ToolParameter(**p) for p in self.parameters],
            category=self.category,
        )

    def _ok(self, data: Any) -> ToolResult:
        return ToolResult(success=True, data=data)

    def _err(self, msg: str) -> ToolResult:
        return ToolResult(success=False, error=msg)


class CurrentTimeTool(_GTool):
    name = "current_time"
    description = "获取当前日期与时间（含时区）。"
    parameters: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        now = datetime.datetime.now().astimezone()
        return self._ok(
            {
                "datetime": now.isoformat(),
                "date": now.strftime("%Y-%m-%d"),
                "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
                "time": now.strftime("%H:%M:%S"),
                "timezone": str(now.tzinfo or "local"),
            }
        )


class CalculateTool(_GTool):
    name = "calculate"
    description = "安全地进行数学计算（四则运算、括号、幂、常用函数）。"
    parameters: list[dict[str, Any]] = [
        {
            "name": "expression",
            "type": "string",
            "required": True,
            "description": "数学表达式，如 (70*1.5)/100",
        },
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        expr = (kwargs.get("expression") or "").strip()
        if not expr:
            return self._err("表达式为空")
        result = _safe_eval(expr)
        if isinstance(result, str):  # error message
            return self._err(result)
        if isinstance(result, bool):
            result = int(result)
        if isinstance(result, float):
            result = round(result, 10)
        return self._ok({"expression": expr, "result": result})


def _safe_eval(expr: str) -> Any:
    """Evaluate an arithmetic expression safely.

    Prefers the mature, sandboxed ``simpleeval`` library; falls back to a
    whitelist-guarded ``eval`` when it is unavailable.
    """
    try:
        import simpleeval  # type: ignore[import-not-found]

        try:
            return simpleeval.simple_eval(
                expr,
                functions={  # allow a few safe math helpers
                    "sqrt": lambda x: __import__("math").sqrt(x),
                    "abs": abs,
                    "round": round,
                    "min": min,
                    "max": max,
                },
            )
        except simpleeval.NameNotDefined:
            return "表达式含未定义名称"
        except simpleeval.InvalidExpression:
            return "表达式非法"
        except Exception as exc:  # noqa: BLE001
            return f"计算失败：{exc}"
    except ImportError:  # pragma: no cover — fallback
        if re.search(r"[^\d\s\.\+\-\*\/\(\)\%\^\*\*\,\s]", expr):
            return "表达式含非法字符"
        try:
            return eval(expr, {"__builtins__": None}, {})  # nosec B307 — 白名单已过滤
        except Exception as exc:  # noqa: BLE001
            return f"计算失败：{exc}"


class WebSearchTool(_GTool):
    name = "web_search"
    description = "联网搜索最新信息（返回标题/链接/摘要）。可配置后端，默认 DuckDuckGo。"
    parameters: list[dict[str, Any]] = [
        {"name": "query", "type": "string", "required": True, "description": "搜索词"},
        {"name": "max_results", "type": "integer", "required": False, "description": "返回条数"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = (kwargs.get("query") or "").strip()
        if not query:
            return self._err("查询词为空")
        limit = int(kwargs.get("max_results") or 5)
        try:
            results = await self._search(query, limit)
        except Exception as exc:  # noqa: BLE001
            logger.exception("web_search failed")
            return self._err(f"搜索失败：{exc}")
        return self._ok({"query": query, "total": len(results), "results": results})

    async def _search(self, query: str, limit: int) -> list[dict[str, str]]:
        import os

        backend = os.environ.get("DOCTORAGENT_SEARCH_URL", "").strip()
        if backend:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(backend, params={"q": query, "format": "json"})
                r.raise_for_status()
                data = r.json()
                results = data.get("results") or []
                # SearXNG names the excerpt field "content"; normalize it to
                # the internal "snippet" shape so any self-hosted meta search
                # engine works without a proxy.
                normalized: list[dict[str, str]] = []
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    row = {
                        "title": str(item.get("title", "")),
                        "url": str(item.get("url", item.get("href", ""))),
                        "snippet": str(item.get("snippet", item.get("content", "")))[:200],
                    }
                    normalized.append(row)
                return normalized[:limit]
        # 优先用成熟的 duckduckgo_search 库
        try:
            from duckduckgo_search import DDGS  # type: ignore[import-not-found]

            out: list[dict[str, str]] = []
            for r in DDGS().text(query, max_results=limit):
                out.append(
                    {
                        "title": r.get("title", ""),
                        "url": r.get("href", ""),
                        "snippet": r.get("body", "")[:200],
                    }
                )
            if out:
                return out
        except Exception:  # noqa: BLE001 — fall through to HTML parsing
            pass
        # 回退：DuckDuckGo HTML 解析
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "DoctorAgent/1.0"}) as c:
            r = await c.get(url)
            r.raise_for_status()
        return _parse_duckduckgo(r.text)[:limit]


def _parse_duckduckgo(html: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html):
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        url = m.group(1)
        results.append({"title": title, "url": url})
    for i, m in enumerate(re.finditer(r'class="result__snippet"[^>]*>(.*?)</a>', html)):
        if i < len(results):
            results[i]["snippet"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()[:200]
    return results


class WebFetchTool(_GTool):
    name = "web_fetch"
    description = "抓取一个网页 URL 并提取可读文本内容。"
    parameters: list[dict[str, Any]] = [
        {"name": "url", "type": "string", "required": True, "description": "网页 URL"},
    ]

    async def execute(self, **kwargs: Any) -> ToolResult:
        url = (kwargs.get("url") or "").strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            return self._err("URL 必须以 http(s):// 开头")
        try:
            async with httpx.AsyncClient(
                timeout=25, follow_redirects=True, headers={"User-Agent": "DoctorAgent/1.0"}
            ) as c:
                r = await c.get(url)
                r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return self._err(f"抓取失败：{exc}")
        text = _html_to_text(r.text)
        return self._ok(
            {"url": url, "status": r.status_code, "title": _title_of(r.text), "text": text[:6000]}
        )


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _title_of(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return m.group(1).strip() if m else ""


_TOOLS = (CurrentTimeTool, CalculateTool, WebSearchTool, WebFetchTool)


def register_general_tools(registry: Any) -> list[str]:
    """Register the general-purpose agent tools."""
    names: list[str] = []
    for cls in _TOOLS:
        t = cls()
        if registry.get(t.name) is None:
            registry.register(t)
            names.append(t.name)
    return names
