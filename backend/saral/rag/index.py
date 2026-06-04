"""Hybrid retrieval index over the policy corpus.

Combines dense (embedding cosine) and lexical (BM25) rankings via Reciprocal Rank Fusion
(RRF). Returns `Passage` objects carrying their source citation. Never generates facts.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from rank_bm25 import BM25Okapi

from saral.config import get_settings
from saral.logging import get_logger
from saral.rag.embedder import (
    Embedder,
    HashingEmbedder,
    SentenceTransformerEmbedder,
    cosine,
    tokenize,
)
from saral.schemas import Passage

log = get_logger(__name__)

RRF_K = 60


def _chunk_document(text: str) -> list[str]:
    """Split a markdown doc into passages on blank lines; drop the bare title line."""
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    chunks: list[str] = []
    for b in blocks:
        # Keep heading attached to following text for context, but skip a lone '# Title'.
        if b.startswith("#") and "\n" not in b:
            continue
        chunks.append(" ".join(b.split()))
    return chunks


def _make_embedder() -> Embedder:
    if get_settings().embedder == "sentence-transformer":
        return SentenceTransformerEmbedder()
    return HashingEmbedder()


class HybridIndex:
    def __init__(self, corpus_dir: str | Path) -> None:
        self.embedder = _make_embedder()
        self.passages: list[Passage] = []
        self._vectors: list[list[float]] = []
        self._bm25: BM25Okapi | None = None
        self._load(Path(corpus_dir))

    def _load(self, corpus_dir: Path) -> None:
        if not corpus_dir.exists():
            log.warning("rag.corpus_missing", dir=str(corpus_dir))
            return
        tokenized: list[list[str]] = []
        for path in sorted(corpus_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for i, chunk in enumerate(_chunk_document(text)):
                self.passages.append(Passage(doc_id=path.name, chunk_id=i, text=chunk))
                self._vectors.append(self.embedder.embed(chunk))
                tokenized.append(tokenize(chunk))
        if tokenized:
            self._bm25 = BM25Okapi(tokenized)
        log.info("rag.index_built", passages=len(self.passages), dir=str(corpus_dir))

    def search(self, query: str, top_k: int | None = None) -> list[Passage]:
        if not self.passages:
            return []
        top_k = top_k or get_settings().retrieval_top_k

        # Dense ranking
        qv = self.embedder.embed(query)
        dense = sorted(
            range(len(self.passages)),
            key=lambda i: cosine(qv, self._vectors[i]),
            reverse=True,
        )
        # Lexical ranking
        lexical = dense
        if self._bm25 is not None:
            scores = self._bm25.get_scores(tokenize(query))
            lexical = sorted(range(len(self.passages)), key=lambda i: scores[i], reverse=True)

        # Reciprocal Rank Fusion
        fused: dict[int, float] = {}
        for rank, idx in enumerate(dense):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank)
        for rank, idx in enumerate(lexical):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank)

        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        out: list[Passage] = []
        for idx, score in ranked:
            p = self.passages[idx].model_copy(update={"score": round(score, 6)})
            out.append(p)
        return out


@lru_cache
def get_index() -> HybridIndex:
    return HybridIndex(get_settings().corpus_dir)


def search_knowledge(query: str, top_k: int | None = None) -> list[Passage]:
    """Tool entrypoint (TRD §14): hybrid semantic + keyword retrieval with citations."""
    return get_index().search(query, top_k=top_k)
