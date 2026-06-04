"""Embeddings for the dense half of hybrid retrieval.

Default `HashingEmbedder` is dependency-free, deterministic, and offline — a hashing-trick
TF vector, L2-normalized so dot product == cosine. Good enough to demonstrate semantic
ranking and fully reproducible for eval. A real `SentenceTransformerEmbedder` is available
via the `embeddings` extra for higher quality.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9ऀ-ॿ]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Hashing-trick term-frequency vector, L2-normalized."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _bucket(self, token: str) -> int:
        h = hashlib.md5(token.encode("utf-8")).digest()  # noqa: S324 — non-crypto use
        return int.from_bytes(h[:4], "big") % self.dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in tokenize(text):
            vec[self._bucket(tok)] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class SentenceTransformerEmbedder:
    """Optional higher-quality embedder (requires the `embeddings` extra)."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = self._model.get_sentence_embedding_dimension()

    def embed(self, text: str) -> list[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))
