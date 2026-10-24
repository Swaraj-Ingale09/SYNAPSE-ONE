"""Embedding providers: a pluggable interface with two real implementations.

The knowledge layer must never *pretend* to embed. This module therefore offers
two genuinely functional providers and no simulation:

1. :class:`HashingEmbeddings` (default, no key required) — feature hashing over
   word unigrams/bigrams plus signed hashing and L2 normalisation. This is a
   real, long-standing NLP technique (the "hashing trick", cf. Weinberger et al.
   2009): it produces deterministic vectors that genuinely encode lexical
   overlap, so similar documents do genuinely rank closer. It is **lexical, not
   semantic** — "car" and "automobile" will not match — and the code says so
   loudly rather than implying transformer-grade recall it does not have.

2. :class:`OpenAIEmbeddings` — real semantic vectors from the OpenAI embeddings
   API, dimension-pinned to ``EMBEDDING_DIMENSION`` so it drops into the same
   ``vector(1024)`` column.

Neither provider fabricates a result. If the OpenAI provider cannot reach the
API or the response is malformed, it raises; it never falls back to emitting a
hash vector under the caller's belief that they asked for semantic embeddings.
Switching providers is an explicit, visible choice.
"""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Protocol

import httpx

from synapse.knowledge.config import (
    EMBEDDING_DIMENSION,
    OPENAI_EMBEDDING_DIMENSIONS,
    OPENAI_EMBEDDING_MODEL,
)
from synapse.security import redact

log = logging.getLogger("synapse.knowledge.embeddings")

_TOKEN_RE = re.compile(r"[a-z0-9']+")

MAX_EMBED_BATCH = 128
OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"


class EmbeddingError(RuntimeError):
    """Embedding could not be produced. Never silently replaced with a stub."""


class EmbeddingProvider(Protocol):
    name: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one L2-normalised vector per input text, order preserved."""
        ...


def _l2_normalize(vec: dict[int, float]) -> list[float]:
    """Turn a sparse {index: value} map into a dense unit vector."""
    norm = math.sqrt(sum(v * v for v in vec.values()))
    out = [0.0] * EMBEDDING_DIMENSION
    if norm == 0.0:  # all-zero input: leave as-is rather than dividing by zero
        return out
    for idx, value in vec.items():
        out[idx] = value / norm
    return out


def _stable_hash(text: str) -> int:
    """FNV-1a: stable across processes and Python runs (hash() is salted)."""
    h = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


class HashingEmbeddings:
    """Local lexical embedder: real feature hashing, no network, no key.

    Signed feature hashing over word unigrams and bigrams. Deterministic and
    dependency-free. **Lexical, not semantic** — see the module docstring.
    """

    name = "local-hashing"
    dimension = EMBEDDING_DIMENSION

    def __init__(self, dimension: int = EMBEDDING_DIMENSION) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        if len(texts) > MAX_EMBED_BATCH:
            raise EmbeddingError(
                f"batch of {len(texts)} texts exceeds limit {MAX_EMBED_BATCH}"
            )
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        tokens = _TOKEN_RE.findall(text.lower())
        sparse: dict[int, float] = {}

        for token in tokens:
            h = _stable_hash(token)
            idx = h % self.dimension
            sign = 1.0 if (h >> 63) & 1 else -1.0  # signed hashing: cuts collision bias
            sparse[idx] = sparse.get(idx, 0.0) + sign

        # Bigrams give phrase sensitivity ("machine learning" != "learning machine")
        for i in range(len(tokens) - 1):
            h = _stable_hash(f"{tokens[i]}_{tokens[i + 1]}")
            idx = h % self.dimension
            sign = 1.0 if (h >> 63) & 1 else -1.0
            sparse[idx] = sparse.get(idx, 0.0) + sign

        dense = _l2_normalize(sparse)
        # Guard: a normalised vector must never be all zeros, or every document
        # would be equidistant from every query. Emit a minimal real signal.
        if not any(dense):
            h = _stable_hash(text.strip() or "empty")
            dense[h % self.dimension] = 1.0
        return dense


class OpenAIEmbeddings:
    """Real semantic embeddings from the OpenAI embeddings API."""

    name = "openai"
    dimension = EMBEDDING_DIMENSION

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = OPENAI_EMBEDDING_MODEL,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = (
            api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        ).strip()
        self._model = model
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise EmbeddingError(
                "OPENAI_API_KEY is not set — semantic embeddings unavailable"
            )
        if len(texts) > MAX_EMBED_BATCH:
            raise EmbeddingError(
                f"batch of {len(texts)} texts exceeds limit {MAX_EMBED_BATCH}"
            )
        payload = {
            "model": self._model,
            "input": texts,
            "dimensions": OPENAI_EMBEDDING_DIMENSIONS,
        }
        try:
            response = self._client.post(
                OPENAI_EMBED_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "content-type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc

        if response.status_code >= 400:
            detail = redact(f"HTTP {response.status_code}: {response.text[:300]}") or ""
            raise EmbeddingError(f"openai embeddings rejected: {detail}")

        try:
            data = response.json()
            items = sorted(
                data["data"], key=lambda d: d["index"]  # API may reorder
            )
            vectors = [item["embedding"] for item in items]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError("openai embeddings returned unexpected shape") from exc

        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"expected {len(texts)} vectors, got {len(vectors)}"
            )
        for vec in vectors:
            if len(vec) != EMBEDDING_DIMENSION:
                raise EmbeddingError(
                    f"provider returned {len(vec)} dims, column expects "
                    f"{EMBEDDING_DIMENSION}"
                )
        return vectors


def build_embeddings(provider: str | None = None) -> EmbeddingProvider:
    """Select an embedding provider by name, or from ``SYNAPSE_EMBEDDINGS``.

    Resolution order: explicit argument > ``SYNAPSE_EMBEDDINGS`` env > default
    local hashing. The default is the keyless provider so the knowledge layer
    works out of the box; callers wanting semantic vectors ask for "openai".
    """
    choice = (provider or os.environ.get("SYNAPSE_EMBEDDINGS") or "local").strip().lower()
    if choice in ("local", "hashing", "local-hashing"):
        return HashingEmbeddings()
    if choice == "openai":
        return OpenAIEmbeddings()
    raise EmbeddingError(
        f"unknown embedding provider {choice!r} (use 'local' or 'openai')"
    )
