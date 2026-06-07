"""Embeddings for the dense half of hybrid retrieval.

Two embedders, split by role (ADR-0003 "Free-only provider stack"):

  - `SentenceTransformerEmbedder` (multilingual-e5) is the LIVE path. A local multilingual
    model is what makes per-customer Hindi/Hinglish retrieval — the differentiator — return
    semantically relevant clauses instead of garbage. e5 is asymmetric: queries are encoded
    with a `query:` prefix and passages with a `passage:` prefix (see embed_query/document).
  - `HashingEmbedder` is the offline/CI floor: dependency-free, deterministic, fully
    reproducible for eval, but a hashing trick has no cross-lingual semantics.

`get_embedder()` picks per config and falls back to the hashing floor if the optional
`embeddings` extra (sentence-transformers) is not installed.
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

from saral.config import get_settings
from saral.logging import get_logger

log = get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9ऀ-ॿ]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def embed_document(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Hashing-trick term-frequency vector, L2-normalized. Symmetric (no query/doc split)."""

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

    # Hashing has no asymmetry; queries and documents embed identically.
    def embed_query(self, text: str) -> list[float]:
        return self.embed(text)

    def embed_document(self, text: str) -> list[float]:
        return self.embed(text)


class SentenceTransformerEmbedder:
    """Local sentence-transformer embedder. Defaults to multilingual-e5 (requires `embeddings`).

    e5 models are trained with asymmetric `query:` / `passage:` prefixes; applying them is
    required for good retrieval. Non-e5 models skip the prefix (detected by name).
    """

    def __init__(self, model_name: str | None = None) -> None:
        from sentence_transformers import SentenceTransformer

        model_name = model_name or get_settings().embedder_model
        self._model = SentenceTransformer(model_name)
        self.dim = self._model.get_sentence_embedding_dimension()
        self._e5 = "e5" in model_name.lower()

    def _encode(self, text: str) -> list[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self._encode(f"query: {text}" if self._e5 else text)

    def embed_document(self, text: str) -> list[float]:
        return self._encode(f"passage: {text}" if self._e5 else text)

    # Back-compat: bare embed() treats input as a document.
    def embed(self, text: str) -> list[float]:
        return self.embed_document(text)


@lru_cache
def get_embedder() -> Embedder:
    """Shared embedder per config. Falls back to the hashing floor if e5 can't be loaded."""
    if get_settings().embedder == "sentence-transformer":
        try:
            emb = SentenceTransformerEmbedder()
            log.info("rag.embedder", kind="sentence-transformer", model=get_settings().embedder_model)  # noqa: E501
            return emb
        except Exception as e:  # noqa: BLE001 — missing extra / model download failure
            log.warning("rag.embedder.fallback", error=str(e), fallback="hashing")
    return HashingEmbedder()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))
