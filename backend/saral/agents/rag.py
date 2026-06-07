"""Knowledge / RAG agent.

Retrieves grounded passages from two sources: the GENERIC corpus (product FAQs, public policy
templates) and the customer's PER-CUSTOMER documents (their actual policy wording, rider
variants, claim adjudication notes). Per-customer retrieval is scoped by the verified,
token-derived `user_id` ∧ the intent→domain allowed set — never by ids from the message body.
It never free-generates facts; synthesis composes the answer constrained to these citations.
"""

from __future__ import annotations

from saral.rag.index import search_knowledge
from saral.schemas import Passage


class RagAgent:
    name = "rag"

    async def run(
        self,
        query: str,
        top_k: int | None = None,
        user_id: str | None = None,
        allowed_domains: list[str] | None = None,
    ) -> list[Passage]:
        return search_knowledge(
            query, top_k=top_k, user_id=user_id, allowed_domains=allowed_domains
        )
