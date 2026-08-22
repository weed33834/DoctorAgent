"""Tests: OpenAI-compatible remote embedding provider (v0.3.13).

Lets the semantic pipeline run against TEI / Ollama / Infinity instead of an
in-process sentence-transformers model. Verified against a mock transport:
batching, auth header, response parsing, error handling.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from doctoragent.model.embedding import (
    OpenAICompatibleEmbeddingProvider,
)

_V1 = "http://tei:80/v1"


def _mock_handler(captured: list[dict[str, Any]]):
    """Build an httpx.MockTransport handler; vector derives from input text."""

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "path": request.url.path,
                "auth": request.headers.get("Authorization"),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        body = json.loads(request.content.decode("utf-8"))
        inputs = body["input"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "object": "embedding",
                        "index": i,
                        # Text-derived so order assertions survive batching.
                        "embedding": [float(ord(t[0]) % 97)] * 4
                        if t
                        else [0.0] * 4,
                    }
                    for i, t in enumerate(inputs)
                ],
                "model": body["model"],
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )

    return httpx.MockTransport(handler)


def _provider(captured: list[dict[str, Any]], **kwargs: Any):
    return OpenAICompatibleEmbeddingProvider(
        base_url=_V1, model="BAAI/bge-m3", transport=_mock_handler(captured), **kwargs
    )


class TestOpenAICompatibleEmbeddingProvider:
    def test_basic_embed(self) -> None:
        captured: list[dict[str, Any]] = []
        vecs = _provider(captured).embed(["华法林与布洛芬"])
        assert len(vecs) == 1
        assert len(vecs[0]) == 4
        assert captured[0]["path"] == "/v1/embeddings"
        assert captured[0]["body"]["model"] == "BAAI/bge-m3"

    def test_batching_and_order(self) -> None:
        captured: list[dict[str, Any]] = []
        texts = ["a", "b", "c", "d", "e"]
        provider = _provider(captured, batch_size=2)
        vecs = provider.embed(texts)
        assert len(vecs) == 5
        expected = [float(ord(t[0]) % 97) for t in texts]
        assert [v[0] for v in vecs] == expected
        assert len(captured) == 3  # ceil(5/2)

    def test_auth_header_when_key_set(self) -> None:
        captured: list[dict[str, Any]] = []
        OpenAICompatibleEmbeddingProvider(
            base_url=_V1, model="m", api_key="sk-test", transport=_mock_handler(captured)
        ).embed(["x"])
        assert captured[0]["auth"] == "Bearer sk-test"

    def test_no_auth_header_without_key(self) -> None:
        captured: list[dict[str, Any]] = []
        _provider(captured).embed(["x"])
        assert captured[0]["auth"] is None

    def test_empty_input_no_request(self) -> None:
        captured: list[dict[str, Any]] = []
        assert _provider(captured).embed([]) == []
        assert captured == []

    def test_http_error_raises(self) -> None:
        import httpx

        def fail(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        provider = OpenAICompatibleEmbeddingProvider(
            base_url=_V1, model="m", transport=httpx.MockTransport(fail)
        )
        with pytest.raises(httpx.HTTPStatusError):
            provider.embed(["x"])

    def test_malformed_payload_raises(self) -> None:
        import httpx

        def bad(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        provider = OpenAICompatibleEmbeddingProvider(
            base_url=_V1, model="m", transport=httpx.MockTransport(bad)
        )
        with pytest.raises(RuntimeError):
            provider.embed(["x"])

    def test_requires_base_url_and_model(self) -> None:
        with pytest.raises(ValueError):
            OpenAICompatibleEmbeddingProvider(base_url="", model="m")
        with pytest.raises(ValueError):
            OpenAICompatibleEmbeddingProvider(base_url=_V1, model="")
