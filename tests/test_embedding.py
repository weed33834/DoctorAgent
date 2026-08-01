# mypy: ignore-errors
"""Tests for local embedding providers."""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from doctoragent.model.embedding import (
    DeterministicEmbeddingProvider,
    LocalEmbeddingProvider,
    SentenceTransformersProvider,
)


def test_deterministic_provider_returns_unit_vectors() -> None:
    """DeterministicEmbeddingProvider returns normalized vectors."""
    provider = DeterministicEmbeddingProvider(dimension=16)
    vectors = provider.embed(["hello", "world"])

    assert len(vectors) == 2
    assert len(vectors[0]) == 16
    assert len(vectors[1]) == 16
    # Vectors should be unit length.
    assert sum(value * value for value in vectors[0]) == pytest.approx(1.0)
    # Different inputs produce different vectors.
    assert vectors[0] != vectors[1]


def test_deterministic_provider_is_stable() -> None:
    """Embedding the same text twice yields the same vector."""
    provider = DeterministicEmbeddingProvider(dimension=16, seed=123)

    first = provider.embed(["doctoragent"])
    second = provider.embed(["doctoragent"])

    assert first == second


def test_deterministic_provider_empty_input() -> None:
    """An empty list returns an empty list."""
    provider = DeterministicEmbeddingProvider(dimension=8)

    assert provider.embed([]) == []


def test_sentence_transformers_provider_raises_when_missing() -> None:
    """SentenceTransformersProvider raises ImportError when dep is missing."""
    with patch.dict(sys.modules, {"sentence_transformers": None}):
        with pytest.raises(ImportError):
            SentenceTransformersProvider()


def test_sentence_transformers_provider_uses_loaded_model() -> None:
    """SentenceTransformersProvider delegates encode to the loaded model."""
    fake_model = MagicMock()
    fake_model.encode.return_value = [[0.1, 0.2, 0.3]]

    fake_module = ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = MagicMock(return_value=fake_model)

    with patch.dict(sys.modules, {"sentence_transformers": fake_module}):
        provider = SentenceTransformersProvider("all-MiniLM-L6-v2")
        vectors = provider.embed(["test"])

    assert vectors == [[0.1, 0.2, 0.3]]
    fake_model.encode.assert_called_once_with(["test"], show_progress_bar=False)


def test_sentence_transformers_provider_model_load_failure() -> None:
    """Model load failures are wrapped in RuntimeError."""
    fake_module = ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = MagicMock(side_effect=OSError("no model"))

    with patch.dict(sys.modules, {"sentence_transformers": fake_module}):
        with pytest.raises(RuntimeError):
            SentenceTransformersProvider("missing-model")


def test_provider_is_abstract() -> None:
    """LocalEmbeddingProvider cannot be instantiated directly."""
    with pytest.raises(TypeError):
        LocalEmbeddingProvider()  # type: ignore[abstract]


def test_custom_provider_can_be_implemented() -> None:
    """A custom provider implementing the interface works."""

    class FixedProvider(LocalEmbeddingProvider):
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    provider = FixedProvider()
    assert provider.embed(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]
