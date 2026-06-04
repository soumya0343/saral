"""Result schemas for the mock backend tools (TRD §14)."""

from __future__ import annotations

from pydantic import BaseModel


class ClaimStatus(BaseModel):
    claim_id: str
    policy_id: str
    status: str  # filed | under_review | approved | rejected | settled
    amount_inr: float | None = None
    last_updated: str
    note: str | None = None


class Policy(BaseModel):
    policy_id: str
    holder_user_id: str
    product: str  # health | motor | term_life | personal_loan
    status: str  # active | lapsed | cancelled
    premium_inr: float
    sum_assured_inr: float
    renewal_date: str


class ActionResult(BaseModel):
    ok: bool
    idempotency_key: str
    detail: str
    changed: dict | None = None


class Ticket(BaseModel):
    ticket_id: str
    user_id: str
    subject: str
    status: str = "open"
    idempotency_key: str
