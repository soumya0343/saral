"""Per-customer document index — the differentiator.

Loads each hero customer's document-fidelity files (policy schedules with numbered clauses,
rider variants, claim adjudication notes, correspondence) from `data/customers/`, tagged with
`customer_id` + data `domain`. Retrieval applies a HARD pre-filter — `customer_id ∧
allowed-domains` — BEFORE scoring (: never post-rank). A query
naming another customer's policy id can never widen this; entities from the message body do
not relax the filter — they can only NARROW it to the claim/policy the customer asked about.

Every other policy and claim the customer holds gets a generated document from its row
(`customer_docs.py`), so non-hero and sandbox customers are grounded too.
"""

from __future__ import annotations

import hashlib
import re
import threading
from functools import lru_cache
from pathlib import Path

import yaml
from rank_bm25 import BM25Okapi

from saral.config import get_settings
from saral.logging import get_logger
from saral.rag.chunking import chunk_markdown
from saral.rag.customer_docs import generate_documents
from saral.rag.embedder import cosine, get_embedder, tokenize
from saral.rag.index import doc_language
from saral.schemas import Passage

log = get_logger(__name__)

RRF_K = 60
_ENTITY_RE = re.compile(r"\b(?:CLM|POL)\d+\b", re.IGNORECASE)


def mentioned_entity_ids(*texts: str | None) -> set[str]:
    """Claim / policy ids the customer named (uppercased)."""
    return {m.upper() for t in texts if t for m in _ENTITY_RE.findall(t)}


class _CustomerPassage:
    __slots__ = ("passage", "customer_id", "vector", "tokens", "about")

    def __init__(
        self,
        passage: Passage,
        customer_id: str,
        vector: list[float],
        tokens: list[str],
        about: frozenset[str],
    ):
        self.passage = passage
        self.customer_id = customer_id
        self.vector = vector
        self.tokens = tokens
        self.about = about  # the claim / policy ids this document is about (may be empty)


def _anchored(items: list[_CustomerPassage], mentioned: set[str]) -> list[_CustomerPassage]:
    """Drop documents about a DIFFERENT claim/policy than the one the customer named.

    "What is the status of CLM2001?" must never be answered from the CLM2010 adjudication note.
    A document is dropped only when the customer named an id of the same kind (claim / policy)
    and the document is about other ids of that kind; untagged docs (correspondence) and docs
    of the other kind are unaffected.
    """
    if not mentioned:
        return items
    out: list[_CustomerPassage] = []
    for it in items:
        kinds = {a[:3] for a in it.about}
        relevant = {m for m in mentioned if m[:3] in kinds}
        if not relevant or it.about & relevant:
            out.append(it)
    return out


class CustomerIndex:
    """Authored hero documents (loaded once) + generated documents for every other policy and
    claim the customer holds (rendered from the core-system rows on first use and re-rendered
    when those rows change — e.g. a claim filed through the agent)."""

    def __init__(self, customers_dir: str | Path) -> None:
        self.embedder = get_embedder()
        self.items: list[_CustomerPassage] = []  # authored
        self._authored_about: dict[str, set[str]] = {}
        self._generated: dict[str, tuple[str, list[_CustomerPassage]]] = {}
        self._lock = threading.Lock()
        self._load(Path(customers_dir))

    def _passages(
        self,
        text: str,
        doc_id: str,
        customer_id: str,
        domain: str,
        language: str,
        about: frozenset[str],
    ) -> list[_CustomerPassage]:
        out = []
        for i, chunk in enumerate(chunk_markdown(text)):
            passage = Passage(
                doc_id=doc_id,
                chunk_id=i,
                text=chunk.text,
                scope="customer",
                domain=domain,
                section=chunk.section,
                clause_id=chunk.clause_id,
                language=language,
            )
            out.append(
                _CustomerPassage(
                    passage,
                    customer_id,
                    self.embedder.embed_document(chunk.indexed_text),
                    tokenize(chunk.indexed_text),
                    about,
                )
            )
        return out

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
                if not domain:
                    # Purpose limitation needs a domain on every per-customer doc; an untagged
                    # doc could never be filtered, so it is not indexed at all.
                    log.warning("rag.customer_doc_no_domain", path=doc["path"], customer=cid)
                    continue
                about = frozenset(str(a).upper() for a in doc.get("about", []) or [])
                self._authored_about.setdefault(cid, set()).update(about)
                self.items.extend(
                    self._passages(
                        text,
                        doc["path"].replace("data/customers/", ""),
                        cid,
                        domain,
                        doc.get("language") or doc_language(text),
                        about,
                    )
                )
        log.info("rag.customer_index.built", passages=len(self.items))

    def _generated_for(self, user_id: str) -> list[_CustomerPassage]:
        """Generated docs for this customer's un-authored policies/claims, rebuilt when the
        rows change (fingerprint), so a newly filed claim is groundable on the next turn."""
        from saral.tools import mock_backend as mb

        try:
            policies = mb.list_policies(user_id)
            claims = mb.list_claims(user_id)
        except Exception as e:  # noqa: BLE001 — no store: authored docs only
            log.warning("rag.customer_rows_unavailable", error=str(e))
            return []
        rows = [p.model_dump_json() for p in policies] + [c.model_dump_json() for c in claims]
        fp = hashlib.sha256("|".join(rows).encode()).hexdigest()
        cached = self._generated.get(user_id)
        if cached and cached[0] == fp:
            return cached[1]
        with self._lock:
            docs = generate_documents(
                user_id, policies, claims, self._authored_about.get(user_id, set())
            )
            items = [
                it
                for d in docs
                for it in self._passages(d.text, d.doc_id, user_id, d.domain, d.language, d.about)
            ]
            self._generated[user_id] = (fp, items)
        return items

    def search(
        self,
        query: str,
        user_id: str,
        allowed_domains: list[str] | None,
        top_k: int | None = None,
        mentioned_ids: set[str] | None = None,
    ) -> list[Passage]:
        top_k = top_k or get_settings().retrieval_top_k
        # Default-deny: no authorized domain (None or empty) means no personal data at all.
        if not allowed_domains:
            return []
        allowed = set(allowed_domains)

        # HARD pre-filter (before scoring): own customer_id ∧ allowed domains ∧ the claim /
        # policy the customer named.
        own = [it for it in self.items if it.customer_id == user_id]
        own += self._generated_for(user_id)
        candidates = _anchored(
            [it for it in own if it.passage.domain in allowed], mentioned_ids or set()
        )
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
    mentioned_ids: set[str] | None = None,
) -> list[Passage]:
    return get_customer_index().search(
        query, user_id, allowed_domains, top_k=top_k, mentioned_ids=mentioned_ids
    )
