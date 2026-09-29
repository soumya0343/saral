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
from saral.rag.chunking import chunk_markdown
from saral.rag.embedder import cosine, get_embedder, tokenize
from saral.schemas import Passage

log = get_logger(__name__)

RRF_K = 60


def doc_language(text: str) -> str:
    """'hi' when the document is mostly Devanagari, else 'en'."""
    letters = [c for c in text if c.isalpha()]
    deva = sum(1 for c in letters if "\u0900" <= c <= "\u097f")
    return "hi" if letters and deva / len(letters) >= 0.3 else "en"


class HybridIndex:
    def __init__(self, corpus_dir: str | Path) -> None:
        self.embedder = get_embedder()
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
            lang = doc_language(text)
            for i, chunk in enumerate(chunk_markdown(text)):
                self.passages.append(
                    Passage(
                        doc_id=path.name,
                        chunk_id=i,
                        text=chunk.text,
                        section=chunk.section,
                        clause_id=chunk.clause_id,
                        language=lang,
                    )
                )
                self._vectors.append(self.embedder.embed_document(chunk.indexed_text))
                tokenized.append(tokenize(chunk.indexed_text))
        if tokenized:
            self._bm25 = BM25Okapi(tokenized)
        log.info("rag.index_built", passages=len(self.passages), dir=str(corpus_dir))

    def search(self, query: str, top_k: int | None = None) -> list[Passage]:
        if not self.passages:
            return []
        top_k = top_k or get_settings().retrieval_top_k

        # Dense ranking + per-passage cosine (the surfaced relevance score, used by the
        # retrieval score-floor groundedness gate .
        qv = self.embedder.embed_query(query)
        cos = {i: cosine(qv, self._vectors[i]) for i in range(len(self.passages))}
        dense = sorted(cos, key=lambda i: cos[i], reverse=True)
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
        # Order by RRF; surface the dense cosine as the relevance score.
        return [
            self.passages[idx].model_copy(update={"score": round(cos[idx], 6)})
            for idx, _ in ranked
        ]


@lru_cache
def get_index() -> HybridIndex:
    return HybridIndex(get_settings().corpus_dir)


def _parallel_stem(doc_id: str) -> str:
    """`claims_process_hi.md` and `claims_process.md` are the same FAQ in two languages."""
    stem = doc_id.rsplit(".", 1)[0]
    return stem[:-3] if stem.endswith("_hi") else stem


def _prefer_language(passages: list[Passage], language: str | None) -> list[Passage]:
    """When an FAQ and its translation both surfaced, keep only the customer's language version
    (Hinglish reads Latin script, so it keeps English). Frees top-k slots for other sources."""
    if not language:
        return passages
    want = "hi" if language == "hi" else "en"
    stems_in_want = {_parallel_stem(p.doc_id) for p in passages if p.language == want}
    return [
        p
        for p in passages
        if p.language == want or _parallel_stem(p.doc_id) not in stems_in_want
    ]


def search_knowledge(
    query: str,
    top_k: int | None = None,
    user_id: str | None = None,
    allowed_domains: list[str] | None = None,
    mentioned_ids: set[str] | None = None,
    language: str | None = None,
) -> list[Passage]:
    """Tool entrypoint: hybrid semantic + keyword retrieval with citations.

    Merges the GENERIC corpus (unscoped) with the customer's PER-CUSTOMER documents, the latter
    hard-filtered by the verified `user_id` ∧ `allowed_domains` and anchored to any claim /
    policy id the customer named (`mentioned_ids`), so a question about CLM2001 is never
    answered from the CLM2010 adjudication note. The per-customer index is a no-op when no
    customer data is loaded, so the generic path is unaffected.
    """
    k = top_k or get_settings().retrieval_top_k
    # Over-fetch so dropping a translation duplicate still leaves k passages.
    generic = _prefer_language(get_index().search(query, top_k=k * 2), language)[:k]

    if not user_id:
        return generic
    from saral.rag.customer_index import search_customer

    personal = _prefer_language(
        search_customer(query, user_id, allowed_domains, top_k=k * 2, mentioned_ids=mentioned_ids),
        language,
    )[:k]
    if not personal:
        return generic
    # Per-customer citations lead (they prove the differentiator), then generic context.
    # (A relevance-aware merge was tried: it only helps on the offline hashing scale; under e5
    # personal and generic cosines sit too close for a ratio to separate them.)
    merged = personal + [p for p in generic if p.doc_id not in {pp.doc_id for pp in personal}]
    return merged[: max(k, len(personal))]


def warm_up() -> None:
    """Load the embedder and build both indexes now, so the first query doesn't pay for it.

    With e5 this is ~model load + corpus embedding (tens of seconds on a small CPU). Blocking —
    call it via `asyncio.to_thread`.
    """
    import time

    from saral.rag.customer_index import get_customer_index

    t0 = time.monotonic()
    generic = get_index()
    personal = get_customer_index()
    generic.embedder.embed_query("warm-up")  # first encode allocates the inference buffers
    log.info(
        "rag.warm",
        seconds=round(time.monotonic() - t0, 1),
        passages=len(generic.passages),
        customer_passages=len(personal.items),
    )
