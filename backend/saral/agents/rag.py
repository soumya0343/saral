"""Knowledge / RAG agent.

Retrieves grounded passages from the policy corpus via hybrid search. Returns ranked
passages with citations; it never free-generates facts — synthesis (Phase 3) composes the
answer constrained to these citations.
"""

from __future__ import annotations

from saral.rag.index import search_knowledge
from saral.schemas import Passage


class RagAgent:
    name = "rag"

    async def run(self, query: str, top_k: int | None = None) -> list[Passage]:
        return search_knowledge(query, top_k=top_k)
