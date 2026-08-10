"""Browser automation tool (M4.8 / M12.10).

A :class:`~doctoragent.model.tools.Tool` that drives a real browser via
Playwright (``playwright`` extra, optional) so the ReAct loop can navigate,
click, fill, extract text and screenshot pages. Guarded import — when Playwright
is not installed the tool reports itself unavailable and the agent degrades
gracefully instead of crashing.

Registered via :func:`register_browser_tool` into a
:class:`~doctoragent.model.tools.ToolRegistry`.
"""

from __future__ import annotations

import logging
from typing import Any

from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

BROWSER_ACTIONS = {
    "navigate": "Open a URL in the browser",
    "screenshot": "Take a screenshot of the current page (returns a data URL)",
    "extract_text": "Extract visible text from the current page",
    "get_title": "Get the current page title and URL",
    "click": "Click a CSS selector",
    "fill": "Fill an input field (CSS selector, value)",
}


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


class BrowserTool(Tool):
    """Playwright-backed web browsing tool for the agent loop."""

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._page: Any = None
        self._browser: Any = None
        self._playwright: Any = None

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser_action",
            description=(
                "Drive a real web browser: navigate to a URL, extract text, "
                "click elements, fill forms, take screenshots. Useful for "
                "information retrieval from live web pages. Requires the "
                "`browser` extra (Playwright)."
            ),
            parameters=[
                ToolParameter(name="action", type="string", required=True,
                              description="One of: " + ", ".join(BROWSER_ACTIONS)),
                ToolParameter(name="url", type="string", required=False,
                              description="URL to navigate to (action=navigate)"),
                ToolParameter(name="selector", type="string", required=False,
                              description="CSS selector (action=click/fill)"),
                ToolParameter(name="value", type="string", required=False,
                              description="Value to fill (action=fill)"),
            ],
            category="browser",
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        if not _playwright_available():
            return ToolResult(
                success=False,
                error="Playwright is not installed. Install with: pip install doctoragent[browser]",
                tool_name="browser_action",
            )
        action = kwargs.get("action", "")
        try:
            result = await self._dispatch(action, kwargs)
            return ToolResult(success=True, data=result, tool_name="browser_action")
        except Exception as exc:  # noqa: BLE001 — surface any browser failure
            logger.exception("browser_action failed")
            return ToolResult(success=False, error=str(exc), tool_name="browser_action")

    async def _dispatch(self, action: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if action not in BROWSER_ACTIONS:
            return {"error": f"unknown action {action!r}; supported: {list(BROWSER_ACTIONS)}"}
        page = await self._ensure_page()
        if action == "navigate":
            await page.goto(kwargs.get("url", ""), timeout=30000)
            await page.wait_for_load_state("domcontentloaded")
            return {"title": await page.title(), "url": page.url}
        if action == "get_title":
            return {"title": await page.title(), "url": page.url}
        if action == "extract_text":
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            return {"text": str(text)[:4000]}
        if action == "screenshot":
            png = await page.screenshot(type="png")
            import base64

            return {"data_url": "data:image/png;base64," + base64.b64encode(png).decode()}
        if action == "click":
            await page.click(kwargs.get("selector", ""))
            return {"clicked": kwargs.get("selector", "")}
        if action == "fill":
            await page.fill(kwargs.get("selector", ""), kwargs.get("value", ""))
            return {"filled": kwargs.get("selector", "")}
        return {"error": "unreachable"}

    async def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        context = await self._browser.new_context(user_agent=(
            "Mozilla/5.0 (compatible; DoctorAgent/0.3; +clinical-agent)"
        ))
        self._page = await context.new_page()
        return self._page

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        self._page = None
        self._browser = None


def register_browser_tool(registry: Any, headless: bool = True) -> str | None:
    """Register :class:`BrowserTool` into *registry*; returns the tool name."""
    name = "browser_action"
    if registry.get(name) is None:
        registry.register(BrowserTool(headless=headless))
    return name
