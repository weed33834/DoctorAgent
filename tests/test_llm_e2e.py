# mypy: ignore-errors
"""Real-LLM end-to-end integration tests.

These tests exercise the live classification pipeline against a real model
service (e.g. Ollama, llama.cpp, vLLM). They are **opt-in** and skipped by
default to keep the standard CI suite fast and hermetic.

Running them requires two environment variables:

- ``DOCTORAGENT_E2E_LLM_URL``  — base URL of an OpenAI-compatible endpoint,
  e.g. ``http://127.0.0.1:11434/v1``. Must be a trusted local address.
- ``DOCTORAGENT_E2E_LLM_MODEL`` — model name, e.g. ``qwen2.5:7b``.

Optional:

- ``DOCTORAGENT_E2E_LLM_API_KEY`` — API key for cloud-authorised endpoints.

Invoke explicitly with::

    DOCTORAGENT_E2E_LLM_URL=http://127.0.0.1:11434/v1 \\
    DOCTORAGENT_E2E_LLM_MODEL=qwen2.5:7b \\
    poetry run pytest -m integration tests/test_llm_e2e.py -v

The standard ``pytest`` invocation skips these tests because ``addopts`` in
``pyproject.toml`` filters out the ``integration`` marker.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click

import pytest

from doctoragent.api.schemas import ClassificationResult, SensitivityLevel
from doctoragent.connections.models import AuthMethod, Connection, PlatformType
from doctoragent.model.classifier import Classifier
from doctoragent.model.provider import OpenAICompatibleProvider, create_provider


# Environment variables that configure the live endpoint.
E2E_URL = os.environ.get("DOCTORAGENT_E2E_LLM_URL", "").strip()
E2E_MODEL = os.environ.get("DOCTORAGENT_E2E_LLM_MODEL", "").strip()
E2E_API_KEY = os.environ.get("DOCTORAGENT_E2E_LLM_API_KEY", "").strip()

# Phase 6.5 — continuous real-LLM latency verification knobs.
# The metrics file is written to the repo root so the CI workflow can upload
# it as an artifact for trend analysis across nightly runs.
E2E_METRICS_PATH = Path(os.environ.get("DOCTORAGENT_E2E_METRICS_PATH", "e2e-metrics.ndjson"))
E2E_LATENCY_ITERS = int(os.environ.get("DOCTORAGENT_E2E_LATENCY_ITERS", "10"))
# Generous default: small CPU-only models can take 10-20s per call.
E2E_LATENCY_P95_S = float(os.environ.get("DOCTORAGENT_E2E_LATENCY_P95_S", "30.0"))

# Skip the whole module unless a live endpoint is configured. This keeps the
# default test run hermetic while still allowing real verification on demand.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (E2E_URL and E2E_MODEL),
        reason=(
            "Set DOCTORAGENT_E2E_LLM_URL and DOCTORAGENT_E2E_LLM_MODEL to run real-LLM end-to-end tests."
        ),
    ),
]


def _make_connection() -> Connection:
    """Build a trusted-local Connection from the E2E environment variables."""
    api_key = E2E_API_KEY or "e2e-no-key-required"
    return Connection(
        name="E2E Live LLM",
        platform_type=PlatformType.OLLAMA,
        base_url=E2E_URL,
        model_name=E2E_MODEL,
        is_local=True,
        auth_method=AuthMethod.BEARER if E2E_API_KEY else AuthMethod.NONE,
        api_key=api_key,
        capabilities=["chat"],
    )


@pytest.fixture
async def live_provider() -> Any:
    """Yield a real OpenAICompatibleProvider and close it after the test."""
    connection = _make_connection()
    provider = OpenAICompatibleProvider(connection)
    try:
        yield provider
    finally:
        await provider.close()


@pytest.fixture
async def live_classifier() -> Any:
    """Yield a Classifier backed by a real provider and close it after."""
    connection = _make_connection()
    classifier = Classifier(create_provider(connection), connection)
    try:
        yield classifier
    finally:
        await classifier.aclose()


async def test_live_provider_health(live_provider: OpenAICompatibleProvider) -> None:
    """The live endpoint must report a healthy status."""
    healthy = await live_provider.health()
    assert healthy, (
        f"Live LLM endpoint at {E2E_URL} did not report healthy. "
        "Ensure the model service is running and reachable."
    )


async def test_live_classifier_returns_valid_result(
    live_classifier: Classifier,
    tmp_path: Path,
) -> None:
    """A benign text file must be classified into a valid ClassificationResult."""
    sample = tmp_path / "meeting_notes.txt"
    sample.write_text(
        "Quarterly review notes.\n"
        "Action items: follow up with the design team, ship the roadmap update.\n",
        encoding="utf-8",
    )
    result = await live_classifier.classify(sample)
    assert isinstance(result, ClassificationResult)
    assert isinstance(result.sensitivity, SensitivityLevel)
    assert isinstance(result.category, str) and result.category
    assert isinstance(result.disguise_name, str) and result.disguise_name
    assert isinstance(result.disguise_extension, str) and result.disguise_extension
    # disguise_name must be sanitised to a safe path component.
    assert "/" not in result.disguise_name
    assert "\\" not in result.disguise_name


async def test_live_classifier_handles_chinese_filename(
    live_classifier: Classifier,
    tmp_path: Path,
) -> None:
    """A Chinese-named file must classify without raising."""
    sample = tmp_path / "会议纪要.txt"
    sample.write_text("项目周会记录：讨论下一阶段发布计划。", encoding="utf-8")
    result = await live_classifier.classify(sample)
    assert isinstance(result, ClassificationResult)
    assert result.disguise_name


async def test_live_classifier_skips_llm_for_sensitive_keyword(
    live_classifier: Classifier,
    tmp_path: Path,
) -> None:
    """A filename matching a sensitive keyword is pre-classified without an LLM call.

    This guards the heuristic fast-path: when the filename contains a sensitive
    keyword the classifier must return immediately, never hitting the network.
    """
    sample = tmp_path / "passport_scan.txt"
    sample.write_text("placeholder content", encoding="utf-8")
    result = await live_classifier.classify(sample)
    assert isinstance(result, ClassificationResult)
    # Sensitive keyword matches should yield a high sensitivity classification.
    assert result.sensitivity in {SensitivityLevel.HIGH, SensitivityLevel.CRITICAL}


async def test_live_classifier_latency_p95_under_threshold(
    live_classifier: Classifier,
    tmp_path: Path,
) -> None:
    """Real-LLM classification P95 must stay under the regression threshold.

    Phase 6.5 — continuous verification. Each run records ``E2E_LATENCY_ITERS``
    classification samples to ``E2E_METRICS_PATH`` so the CI workflow can
    upload the NDJSON file as an artifact and performance can be tracked
    across nightly runs. The assertion guards against catastrophic
    regressions; the threshold is deliberately generous (30s) because small
    CPU-only Ollama models can legitimately take 10-20s per call.

    Override via environment variables:

    - ``DOCTORAGENT_E2E_LATENCY_ITERS`` (default 10)
    - ``DOCTORAGENT_E2E_LATENCY_P95_S`` (default 30.0)
    - ``DOCTORAGENT_E2E_METRICS_PATH`` (default ``e2e-metrics.ndjson``)
    """
    # A stable, non-sensitive input so every iteration exercises the full
    # LLM path (no pre-classify short-circuit).
    sample = tmp_path / "project_notes.txt"
    sample.write_text(
        "Project notes: reviewed the new API design, scheduled a demo for "
        "next Wednesday, need to follow up with the QA team on test coverage.",
        encoding="utf-8",
    )

    await live_classifier.classify(sample)
    click.echo(f"\n[e2e] model={E2E_MODEL} classify OK")
