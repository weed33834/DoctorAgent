"""Model pricing & cost-billing support (M21).

A real model price table + comparison utility on top of the existing
:class:`~doctoragent.model.cost_tracker.CostTracker`. Provides:

* a default price list (USD per 1K tokens, per model/provider) for common
  OpenAI-compatible and local models;
* price lookup and a model comparison (cost per 1K, context window, relative
  capability) used by the "model比价器" capability;
* helpers to project a session's cost from token usage.

Prices are defaults; operators can override via
``DOCTORAGENT_PRICING__MODEL_PRICES``.
"""

from __future__ import annotations

from typing import Any

# USD per 1K tokens (input, output) + context window + capability tier.
DEFAULT_MODEL_PRICES: dict[str, dict[str, Any]] = {
    "gpt-4o": {"input": 0.0025, "output": 0.0100, "context": 128000, "tier": "high"},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.00060, "context": 128000, "tier": "medium"},
    "gpt-4-turbo": {"input": 0.0100, "output": 0.0300, "context": 128000, "tier": "high"},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015, "context": 16385, "tier": "low"},
    "o1": {"input": 0.0150, "output": 0.0600, "context": 200000, "tier": "high"},
    "o1-mini": {"input": 0.0030, "output": 0.0120, "context": 128000, "tier": "medium"},
    "claude-3-5-sonnet": {"input": 0.0030, "output": 0.0150, "context": 200000, "tier": "high"},
    "claude-3-opus": {"input": 0.0150, "output": 0.0750, "context": 200000, "tier": "high"},
    "claude-3-sonnet": {"input": 0.0030, "output": 0.0150, "context": 200000, "tier": "medium"},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125, "context": 200000, "tier": "medium"},
    "deepseek-v3": {"input": 0.00027, "output": 0.00110, "context": 64000, "tier": "medium"},
    "deepseek-r1": {"input": 0.00055, "output": 0.00219, "context": 64000, "tier": "high"},
    "qwen2.5-7b": {
        "input": 0.00010,
        "output": 0.00030,
        "context": 32768,
        "tier": "low",
        "local": True,
    },
    "llama3.1-8b": {
        "input": 0.00010,
        "output": 0.00030,
        "context": 128000,
        "tier": "low",
        "local": True,
    },
    "mistral-7b": {
        "input": 0.00010,
        "output": 0.00030,
        "context": 32768,
        "tier": "low",
        "local": True,
    },
    # Fallback for unknown models.
    "default": {"input": 0.001, "output": 0.002, "context": 0, "tier": "unknown"},
}


def _as_prices(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict) and raw:
        merged = dict(DEFAULT_MODEL_PRICES)
        for model, spec in raw.items():
            merged[model] = {**DEFAULT_MODEL_PRICES.get(model, {}), **spec}
        return merged
    return dict(DEFAULT_MODEL_PRICES)


class ModelPricing:
    """Model price table + comparison helper (M21)."""

    def __init__(self, model_prices: dict[str, dict[str, Any]] | None = None) -> None:
        self.prices = _as_prices(model_prices)

    def lookup(self, model: str) -> dict[str, Any] | None:
        for key, spec in self.prices.items():
            if key in model:  # prefix match, e.g. "gpt-4o-2024-08-06"
                return {"model": key, **spec}
        return None

    def cost_per_1k(self, model: str, input_tokens: int, output_tokens: int) -> float:
        spec = self.lookup(model)
        if spec is None:
            return 0.0
        return spec["input"] * input_tokens / 1000 + spec["output"] * output_tokens / 1000

    def compare(self, models: list[str]) -> list[dict[str, Any]]:
        """Compare the given models on price / context / tier (model比价器)."""
        result: list[dict[str, Any]] = []
        for m in models:
            spec = self.lookup(m)
            if spec is None:
                continue
            result.append(
                {
                    "model": spec["model"],
                    "input_per_1k_usd": spec["input"],
                    "output_per_1k_usd": spec["output"],
                    "context_tokens": spec.get("context", 0),
                    "tier": spec.get("tier", "?"),
                    "local": spec.get("local", False),
                    # combined cost of a typical 1K-in / 1K-out exchange
                    "example_cost_usd": round(spec["input"] + spec["output"], 5),
                }
            )
        # cheaper first
        result.sort(key=lambda x: x["example_cost_usd"])
        return result

    def list_prices(self) -> dict[str, dict[str, Any]]:
        return self.prices


def build_pricing(config: Any = None) -> ModelPricing:
    """Build :class:`ModelPricing` from config (or defaults)."""
    raw = None
    if config is not None:
        raw = getattr(config, "model_prices", None)
    return ModelPricing(raw)
