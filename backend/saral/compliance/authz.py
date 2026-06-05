"""Deterministic authorization.

Correctness matters more than flexibility here (TRD §11.3), so authorization is pure
logic, not LLM judgment. Rule: a user may act only on accounts/policies/claims they own.
"""

from __future__ import annotations

from saral.tools import mock_backend as mb

# Actions that change state or expose account data require an ownership check.
_OWNERSHIP_GATED = {
    "get_claim_status", "get_policy_details", "update_contact", "raise_ticket", "file_claim",
}


def check_authorization(user_id: str, action: str, entities: dict) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    if action not in _OWNERSHIP_GATED:
        return False, f"unknown or non-permitted action '{action}'"

    # update_contact / raise_ticket / file_claim act on the requester's own account.
    if action in ("update_contact", "raise_ticket", "file_claim"):
        return True, "self-service action on own account"

    if action == "get_policy_details":
        policy_id = entities.get("policy_id")
        if not policy_id:
            return True, "no policy referenced yet"  # arg re-ask handled by action agent
        try:
            policy = mb.get_policy_details(policy_id)
        except mb.ToolError:
            return True, "policy not found (handled downstream)"
        if policy.holder_user_id != user_id:
            return False, f"user {user_id} is not the holder of {policy_id}"
        return True, "owns referenced policy"

    if action == "get_claim_status":
        claim_id = entities.get("claim_id")
        if not claim_id:
            return True, "no claim referenced yet"
        try:
            claim = mb.get_claim_status(claim_id)
            policy = mb.get_policy_details(claim.policy_id)
        except mb.ToolError:
            return True, "claim not found (handled downstream)"
        if policy.holder_user_id != user_id:
            return False, f"user {user_id} is not the holder of claim {claim_id}"
        return True, "owns referenced claim"

    return False, "unhandled action"
