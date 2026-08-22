"""Local embedding model provider abstraction.

The core package does not depend on ``sentence-transformers``. Install the
``[semantic]`` extra to use the default local model implementation.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import threading
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

_EMBED_BATCH_SIZE = 32


class LocalEmbeddingProvider(ABC):
    """Abstract local embedding provider."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one dense vector for each input text."""

    def close(self) -> None:  # noqa: B027
        """Optional resource-release hook.

        The base implementation is intentionally a no-op: providers that hold
        no external state (e.g. :class:`DeterministicEmbeddingProvider`) are
        not required to override it. Providers with real resources (e.g.
        :class:`SentenceTransformersProvider`) override to release them.
        """


class SentenceTransformersProvider(LocalEmbeddingProvider):
    """sentence-transformers backed embedding provider.

    The external dependency is imported lazily inside ``__init__`` so that the
    class can be imported without ``sentence-transformers`` installed.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self.model_name = model_name
        self._lock = threading.Lock()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is not installed. "
                "Install it with: pip install doctoragent[semantic]"
            ) from exc
        try:
            self._model: Any = SentenceTransformer(model_name)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load embedding model {model_name!r}. "
                "Ensure the model is downloaded or network is available."
            ) from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Encode texts using the loaded sentence-transformers model.

        Processes in batches of 32 to avoid OOM on large inputs.
        Thread-safe via a lock since PyTorch inference is not thread-safe.
        """
        if not texts:
            return []
        all_results: list[list[float]] = []
        with self._lock:
            for i in range(0, len(texts), _EMBED_BATCH_SIZE):
                batch = texts[i : i + _EMBED_BATCH_SIZE]
                embeddings: Any = self._model.encode(batch, show_progress_bar=False)
                if hasattr(embeddings, "tolist"):
                    all_results.extend(embeddings.tolist())
                else:
                    all_results.extend(list(v) for v in embeddings)
        return all_results

    def close(self) -> None:
        """Release model resources."""
        if hasattr(self._model, "to"):
            try:
                # Move model to CPU to free GPU memory.
                self._model.to("cpu")
            except Exception:
                logger.debug("Failed to move embedding model to CPU", exc_info=True)


class OpenAICompatibleEmbeddingProvider(LocalEmbeddingProvider):
    """Embedding provider for any OpenAI-compatible ``/v1/embeddings`` endpoint.

    Lets the semantic pipeline run against a dedicated inference service —
    [TEI](https://github.com/huggingface/text-embeddings-inference),
    [Ollama](https://github.com/ollama/ollama) (``/v1/embeddings``),
    [Infinity](https://github.com/michaelfeil/infinity), or any gateway that
    speaks the protocol — instead of loading a model in-process.

    The ``httpx`` dependency is imported lazily; it is already part of the
    core dependency set.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 30.0,
        batch_size: int = _EMBED_BATCH_SIZE,
        transport: Any = None,
    ) -> None:
        import httpx

        if not base_url:
            raise ValueError("base_url is required for OpenAICompatibleEmbeddingProvider")
        if not model:
            raise ValueError("model is required for OpenAICompatibleEmbeddingProvider")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.batch_size = max(1, batch_size)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # ``transport`` is an injection point for httpx.MockTransport (tests).
        kwargs: dict[str, Any] = {}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout_seconds, **kwargs
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Encode texts in batches via POST /embeddings."""
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            resp = self._client.post(
                "/embeddings", json={"model": self.model, "input": batch}
            )
            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list) or len(data) != len(batch):
                raise RuntimeError(
                    f"Embedding endpoint returned {len(data) if isinstance(data, list) else 'no'} "
                    f"vectors for {len(batch)} inputs"
                )
            # API returns vectors sorted by index; sort defensively anyway.
            data.sort(key=lambda item: item.get("index", 0))
            for item in data:
                vec = item.get("embedding")
                if not isinstance(vec, list):
                    raise RuntimeError("Embedding response missing 'embedding' vector")
                out.append([float(v) for v in vec])
        return out

    def close(self) -> None:
        self._client.close()


class DeterministicEmbeddingProvider(LocalEmbeddingProvider):
    """Deterministic embedding provider for tests and offline fallback demos.

    Vectors are produced from the SHA-256 hash of the input text and are
    normalized to unit length. They are *not* semantically meaningful.
    """

    def __init__(self, dimension: int = 384, seed: int = 0) -> None:
        if dimension <= 0 or dimension > 8192:
            raise ValueError("dimension must be between 1 and 8192")
        self.dimension = dimension
        self.seed = seed

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return deterministic pseudo-random unit vectors for the texts."""
        return [self._vector_for_text(text) for text in texts]

    def _vector_for_text(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rng = random.Random(f"{self.seed}:{digest}")
        vector = [rng.random() for _ in range(self.dimension)]
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]
