"""Image generation tool (M12.17).

A :class:`~doctoragent.model.tools.Tool` that calls any OpenAI-compatible image
endpoint (``/v1/images/generations``) to create an image from a text prompt.
Guarded import / graceful failure when no endpoint is configured or the request
fails. Registered via :func:`register_image_gen_tool`.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from doctoragent.model.tools import Tool, ToolDefinition, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return urljoin(base + "/", "images/generations")


class ImageGenTool(Tool):
    """Text → image via an OpenAI-compatible images endpoint."""

    def __init__(self, base_url: str = "", model: str = "", api_key: str = "") -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="generate_image",
            description=(
                "Generate an image from a text prompt using a configured "
                "image model endpoint. Returns a base64 data URL of the PNG."
            ),
            parameters=[
                ToolParameter(name="prompt", type="string", required=True,
                              description="Text description of the desired image"),
                ToolParameter(name="size", type="string", required=False,
                              description="Size, e.g. 1024x1024", default="1024x1024"),
            ],
            category="image",
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        if not self.available:
            return ToolResult(
                success=False,
                error="Image generation is not configured (set base_url + model)",
                tool_name="generate_image",
            )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "prompt": kwargs.get("prompt", ""),
            "size": kwargs.get("size", "1024x1024"),
        }
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(_endpoint(self.base_url), headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.exception("generate_image failed")
            return ToolResult(success=False, error=str(exc), tool_name="generate_image")
        items = data.get("data") or []
        if not items:
            return ToolResult(success=False, error="no image returned", tool_name="generate_image")
        url = items[0].get("url", "")
        b64 = items[0].get("b64_json", "")
        if b64:
            return ToolResult(success=True, data={"data_url": f"data:image/png;base64,{b64}"},
                              tool_name="generate_image")
        return ToolResult(success=True, data={"url": url}, tool_name="generate_image")


def register_image_gen_tool(registry: Any, base_url: str = "", model: str = "",
                            api_key: str = "") -> str | None:
    """Register :class:`ImageGenTool` into *registry*."""
    name = "generate_image"
    if registry.get(name) is None:
        registry.register(ImageGenTool(base_url=base_url, model=model, api_key=api_key))
    return name
