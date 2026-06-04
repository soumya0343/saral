"""Mock backend APIs (TRD §14).

In-memory stand-ins for an insurer's core systems. Read-only tools (`get_claim_status`,
`get_policy_details`) and state-changing tools (`update_contact`, `raise_ticket`). State-
changing calls take an idempotency key: a repeated key returns the original result without
re-applying the change — the pattern reused from prior webhook work.

A `ToolError` is raised for not-found / invalid inputs so the Action agent can re-ask.
"""

from __future__ import annotations

from saral.tools.schemas import ActionResult, ClaimStatus, Policy, Ticket


class ToolError(RuntimeError):
    """Recoverable tool error (not found, invalid arg) — caller may re-ask."""


# --- Seed data ---

_USERS: dict[str, dict] = {
    "U1001": {"name": "Asha Verma", "mobile": "9876500001", "email": "asha@example.com"},
    "U1002": {"name": "Rahul Singh", "mobile": "9876500002", "email": "rahul@example.com"},
    "U1003": {"name": "Meena Kumari", "mobile": "9876500003", "email": "meena@example.com"},
}

_POLICIES: dict[str, Policy] = {
    "POL1001": Policy(
        policy_id="POL1001", holder_user_id="U1001", product="health", status="active",
        premium_inr=14500, sum_assured_inr=500000, renewal_date="2026-11-01",
    ),
    "POL1002": Policy(
        policy_id="POL1002", holder_user_id="U1002", product="motor", status="active",
        premium_inr=8200, sum_assured_inr=300000, renewal_date="2026-08-15",
    ),
    "POL1003": Policy(
        policy_id="POL1003", holder_user_id="U1003", product="term_life", status="lapsed",
        premium_inr=22000, sum_assured_inr=5000000, renewal_date="2026-02-01",
    ),
}

_CLAIMS: dict[str, ClaimStatus] = {
    "CLM2001": ClaimStatus(
        claim_id="CLM2001", policy_id="POL1001", status="under_review", amount_inr=45000,
        last_updated="2026-05-28", note="Awaiting hospital discharge summary.",
    ),
    "CLM2002": ClaimStatus(
        claim_id="CLM2002", policy_id="POL1002", status="approved", amount_inr=18000,
        last_updated="2026-05-30", note="Approved; disbursal in 3-5 working days.",
    ),
}

# Idempotency ledger: key -> prior result payload
_IDEMPOTENCY: dict[str, dict] = {}
_TICKET_SEQ = {"n": 3000}


# --- Read-only tools ---

def get_claim_status(claim_id: str) -> ClaimStatus:
    claim = _CLAIMS.get(claim_id.upper())
    if claim is None:
        raise ToolError(f"claim {claim_id} not found")
    return claim


def get_policy_details(policy_id: str) -> Policy:
    policy = _POLICIES.get(policy_id.upper())
    if policy is None:
        raise ToolError(f"policy {policy_id} not found")
    return policy


# --- State-changing tools (idempotent) ---

_ALLOWED_CONTACT_FIELDS = {"mobile", "email"}


def update_contact(user_id: str, field: str, value: str, idempotency_key: str) -> ActionResult:
    if idempotency_key in _IDEMPOTENCY:
        return ActionResult.model_validate(_IDEMPOTENCY[idempotency_key])
    if user_id not in _USERS:
        raise ToolError(f"user {user_id} not found")
    if field not in _ALLOWED_CONTACT_FIELDS:
        raise ToolError(f"field '{field}' not updatable; allowed: {_ALLOWED_CONTACT_FIELDS}")
    old = _USERS[user_id].get(field)
    _USERS[user_id][field] = value
    result = ActionResult(
        ok=True,
        idempotency_key=idempotency_key,
        detail=f"{field} updated",
        changed={"field": field, "old": old, "new": value},
    )
    _IDEMPOTENCY[idempotency_key] = result.model_dump()
    return result


def raise_ticket(user_id: str, subject: str, body: str, idempotency_key: str) -> Ticket:
    if idempotency_key in _IDEMPOTENCY:
        return Ticket.model_validate(_IDEMPOTENCY[idempotency_key])
    if user_id not in _USERS:
        raise ToolError(f"user {user_id} not found")
    _TICKET_SEQ["n"] += 1
    ticket = Ticket(
        ticket_id=f"TKT{_TICKET_SEQ['n']}",
        user_id=user_id,
        subject=subject,
        idempotency_key=idempotency_key,
    )
    _IDEMPOTENCY[idempotency_key] = ticket.model_dump()
    return ticket


def _reset_state() -> None:
    """Test helper: clear idempotency ledger + ticket counter."""
    _IDEMPOTENCY.clear()
    _TICKET_SEQ["n"] = 3000
