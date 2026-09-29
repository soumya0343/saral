"""Mock backend APIs — thin facade over the durable `Store`.

Read-only tools (`get_claim_status`, `get_policy_details`) and state-changing tools
(`update_contact`, `raise_ticket`, `file_claim`) plus customer identity. All account state
lives in the `Store` (Postgres live, in-memory SQLite for tests), so filed claims, raised
tickets, and contact updates persist across restarts. State-changing calls are idempotent.
"""

from __future__ import annotations

from saral.tools.schemas import ActionResult, ClaimStatus, Policy, Ticket
from saral.tools.store import ToolError, get_store

__all__ = [
    "ToolError",
    "get_claim_status",
    "get_policy_details",
    "list_claims",
    "list_policies",
    "update_contact",
    "raise_ticket",
    "file_claim",
    "identify_customer",
    "find_customer",
    "get_customer",
    "revoke_user_sessions",
    "get_customer_context",
]


def get_claim_status(claim_id: str) -> ClaimStatus:
    return get_store().get_claim_status(claim_id)


def get_policy_details(policy_id: str) -> Policy:
    return get_store().get_policy_details(policy_id)


def list_claims(user_id: str) -> list[ClaimStatus]:
    return get_store().list_claims(user_id)


def list_policies(user_id: str) -> list[Policy]:
    return get_store().list_policies(user_id)


def update_contact(user_id: str, field: str, value: str, idempotency_key: str) -> ActionResult:
    return get_store().update_contact(user_id, field, value, idempotency_key)


def raise_ticket(user_id: str, subject: str, body: str, idempotency_key: str) -> Ticket:
    return get_store().raise_ticket(user_id, subject, body, idempotency_key)


def file_claim(user_id: str, subject: str, idempotency_key: str) -> ClaimStatus:
    return get_store().file_claim(user_id, subject, idempotency_key)


def identify_customer(name: str, mobile: str | None = None, email: str | None = None) -> dict:
    return get_store().identify_customer(name, mobile, email)


def find_customer(mobile: str | None = None, email: str | None = None) -> dict | None:
    return get_store().find_customer(mobile, email)


def get_customer(user_id: str) -> dict | None:
    return get_store().get_customer(user_id)


def revoke_user_sessions(user_id: str) -> None:
    get_store().revoke_user_sessions(user_id)


def get_customer_context(user_id: str) -> dict:
    return get_store().get_customer_context(user_id)


def user_exists(user_id: str) -> bool:
    return get_store().user_exists(user_id)


def _reset_state() -> None:
    """Test helper: wipe + reseed the store (and the RM request queue beside it)."""
    from saral.rm.requests import get_requests

    get_store().reset()
    get_requests().reset()
