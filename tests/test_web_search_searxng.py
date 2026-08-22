"""Tests: SearXNG compatibility for the pluggable web_search backend (v0.3.14).

``DOCTORAGENT_SEARCH_URL`` now speaks both the native JSON shape
(``{results:[{title, url, snippet}]}``) and a self-hosted SearXNG instance
with ``format=json`` enabled — SearXNG's ``content`` field is normalized to
the internal ``snippet`` shape without needing a proxy.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def search_tool(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("DOCTORAGENT_SEARCH_URL", raising=False)
    from doctoragent.tools.general_tools import WebSearchTool

    return WebSearchTool()


def _wire_backend(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    captured: list[dict[str, Any]],
    backend_url: str = "http://searx:8080/search",
) -> None:
    """Route WebSearchTool's httpx calls to a mock SearXNG/JSON backend."""

    import httpx

    monkeypatch.setenv("DOCTORAGENT_SEARCH_URL", backend_url)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "params": dict(request.url.params)})
        return httpx.Response(200, json=payload)

    real_init = httpx.AsyncClient.__init__

    def patched(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


class TestSearXNGBackend:
    @pytest.mark.asyncio
    async def test_content_field_normalized_to_snippet(
        self, search_tool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SearXNG returns ``content``; the tool must surface it as snippet."""
        captured: list[dict[str, Any]] = []
        _wire_backend(
            monkeypatch,
            {
                "query": "华法林",
                "results": [
                    {
                        "title": "Warfarin - NIH",
                        "url": "https://www.ncbi.nlm.nih.gov/books/NBK551474/",
                        "content": "Warfarin is a vitamin K antagonist…",
                    }
                ],
            },
            captured,
        )
        result = await search_tool.execute(query="华法林", max_results=3)
        assert result.success is True
        results = result.data["results"]
        assert results[0]["snippet"] == "Warfarin is a vitamin K antagonist…"
        assert results[0]["url"].startswith("https://")
        # format=json requested so SearXNG answers with JSON.
        assert captured[0]["params"].get("format") == "json"

    @pytest.mark.asyncio
    async def test_native_snippet_still_works(
        self, search_tool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The original {title,url,snippet} contract keeps working."""
        _wire_backend(
            monkeypatch,
            {"results": [{"title": "T", "url": "https://example.com", "snippet": "S"}]},
            [],
        )
        result = await search_tool.execute(query="q", max_results=3)
        assert result.success is True
        assert result.data["results"][0] == {
            "title": "T",
            "url": "https://example.com",
            "snippet": "S",
        }

    @pytest.mark.asyncio
    async def test_mixed_and_non_dict_rows_tolerated(
        self, search_tool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire_backend(
            monkeypatch,
            {
                "results": [
                    {"title": "ok", "url": "https://a", "snippet": "s"},
                    "garbage-row",
                    {"title": "href-style", "href": "https://b", "content": "c"},
                ]
            },
            [],
        )
        result = await search_tool.execute(query="q", max_results=5)
        results = result.data["results"]
        assert len(results) == 2
        assert results[1]["url"] == "https://b"
        assert results[1]["snippet"] == "c"

    @pytest.mark.asyncio
    async def test_limit_applied(
        self, search_tool: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [
            {"title": f"t{i}", "url": f"https://x/{i}", "content": "c"} for i in range(10)
        ]
        _wire_backend(monkeypatch, {"results": rows}, [])
        result = await search_tool.execute(query="q", max_results=3)
        assert len(result.data["results"]) == 3
