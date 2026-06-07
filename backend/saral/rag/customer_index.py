"""Per-customer document index (TRD §12.3, FR-16) — the differentiator.

Loads each hero customer's document-fidelity files (policy schedules with numbered clauses,
rider variants, claim adjudication notes, correspondence) from `data/customers/`, tagged with
`customer_id` + data `domain`. Retrieval applies a HARD pre-filter — `customer_id ∧
allowed-domains` — BEFORE scoring (CONTEXT 'Per-customer scope': never post-rank). A query
naming another customer's policy id can never widen this; entities from the message body do
not relax the filter.

No-op (returns []) when no manifest is present, so the generic corpus path is unaffected.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from rank_bm25 import BM25Okapi

from saral.config import get_settings
from saral.logging import get_logger
from saral.rag.embedder import cosine, get_embedder, tokenize
from saral.schemas import Passage

log = get_logger(__name__)

RRF_K = 60


def _chunk(text: str) -> list[str]:
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    out: list[str] = []
    for b in blocks:
        if b.startswith("#") and "\n" not in b:
            continue
        out.append(" ".join(b.split()))
    return out


class _CustomerPassage:
    __slots__ = ("passage", "customer_id", "vector", "tokens")

    def __init__(self, passage: Passage, customer_id: str, vector: list[float], tokens: list[str]):
        self.passage = passage
        self.customer_id = customer_id
        self.vector = vector
        self.tokens = tokens


class CustomerIndex:
    def __init__(self, customers_dir: str | Path) -> None:
        self.embedder = get_embedder()
        self.items: list[_CustomerPassage] = []
        self._load(Path(customers_dir))

    def _load(self, root: Path) -> None:
        manifest = root / "manifest.yaml"
        if not manifest.exists():
            log.info("rag.customer_index.empty", dir=str(root))
            return
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        for cust in data.get("customers", []):
            if not cust.get("is_hero"):
                continue
            cid = cust["customer_id"]
            for doc in cust.get("documents", []):
                # `path` is repo-relative; resolve from cwd as the corpus loader does.
                p = Path(doc["path"])
                if not p.exists():
                    log.warning("rag.customer_doc_missing", path=doc["path"], customer=cid)
                    continue
                text = p.read_text(encoding="utf-8")
                domain = doc.get("domain")
                for i, chunk in enumerate(_chunk(text)):
                    passage = Passage(
                        doc_id=doc["path"].replace("data/customers/", ""),
                        chunk_id=i,
                        text=chunk,
                        scope="customer",
                        domain=domain,
                    )
                    self.items.append(
                        _CustomerPassage(
                            passage, cid, self.embedder.embed_document(chunk), tokenize(chunk)
                        )
                    )
        log.info("rag.customer_index.built", passages=len(self.items))

    def search(
        self,
        query: str,
        user_id: str,
        allowed_domains: list[str] | None,
        top_k: int | None = None,
    ) -> list[Passage]:
        if not self.items:
            return []
        top_k = top_k or get_settings().retrieval_top_k
        allowed = set(allowed_domains) if allowed_domains else None

        # HARD pre-filter (before scoring): own customer_id ∧ allowed domains.
        candidates = [
            it
            for it in self.items
            if it.customer_id == user_id
            and (allowed is None or it.passage.domain is None or it.passage.domain in allowed)
        ]
        if not candidates:
            return []

        qv = self.embedder.embed_query(query)
        cos = {i: cosine(qv, candidates[i].vector) for i in range(len(candidates))}
        dense = sorted(cos, key=lambda i: cos[i], reverse=True)
        bm = BM25Okapi([c.tokens for c in candidates])
        scores = bm.get_scores(tokenize(query))
        lexical = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)

        fused: dict[int, float] = {}
        for rank, idx in enumerate(dense):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank)
        for rank, idx in enumerate(lexical):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank)

        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        # Order by RRF; surface the dense cosine as the relevance score (score-floor gate).
        return [
            candidates[idx].passage.model_copy(update={"score": round(cos[idx], 6)})
            for idx, _ in ranked
        ]


@lru_cache
def get_customer_index() -> CustomerIndex:
    return CustomerIndex(get_settings().customers_dir)


def search_customer(
    query: str,
    user_id: str,
    allowed_domains: list[str] | None = None,
    top_k: int | None = None,
) -> list[Passage]:
    return get_customer_index().search(query, user_id, allowed_domains, top_k=top_k)
