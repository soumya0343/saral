"""Intent→domain map + action risk tiers ('Action risk tier').

This table — code, not LLM — IS the DPDP purpose-limitation boundary. Triage *classifies*;
this map *authorizes* which data domains an intent may read; retrieval enforces
`customer_id ∧ allowed-domains` as a hard pre-filter (rag/customer_index.py). Because the LLM
never controls this map, a prompt-injection cannot widen scope. Default-deny on unmapped
intents.
"""

from __future__ import annotations

from enum import StrEnum

from saral.schemas import Intent, IntentType


class DataDomain(StrEnum):
    """The five customer-scoped partitions."""

    POLICY_COVERAGE = "policy_coverage"
    CLAIMS = "claims"
    BILLING = "billing"
    INTERACTION_HISTORY = "interaction_history"
    PROFILE_ACCOUNT = "profile_account"


# Per-action data domains it may touch. Default-deny: an action absent here authorizes nothing.
_ACTION_DOMAINS: dict[str, set[DataDomain]] = {
    "get_policy_details": {DataDomain.POLICY_COVERAGE},
    "get_claim_status": {DataDomain.CLAIMS},
    "update_contact": {DataDomain.PROFILE_ACCOUNT},
    "raise_ticket": {DataDomain.INTERACTION_HISTORY},
    "file_claim": {DataDomain.CLAIMS},
}

# An information (FAQ/policy) question may read the customer's readable domains, but never
# their profile/account writes-only surface or raw interaction history (those need an action).
_INFORMATION_DOMAINS: set[DataDomain] = {
    DataDomain.POLICY_COVERAGE,
    DataDomain.CLAIMS,
    DataDomain.BILLING,
}

# Action risk tier: all state-changing tools require step-up; all reads do not.
# `update_contact` is the canonical account-takeover vector and the load-bearing gate.
_STEP_UP_ACTIONS: set[str] = {"update_contact", "raise_ticket", "file_claim"}


def requires_step_up(action: str) -> bool:
    return action in _STEP_UP_ACTIONS


def domains_for_intents(intents: list[Intent], include_history: bool = False) -> set[DataDomain]:
    """Union of data domains authorized by the classified intents. Default-deny.

    Long-term memory is the Interaction-history domain retrieved
    *on demand*: it is authorized only when the customer's information question explicitly seeks
    past interactions (`include_history`), never eagerly — preserving data minimization.
    """
    allowed: set[DataDomain] = set()
    has_info = False
    for intent in intents:
        if intent.type == IntentType.INFORMATION:
            allowed |= _INFORMATION_DOMAINS
            has_info = True
        elif intent.type == IntentType.ACTION and intent.action:
            allowed |= _ACTION_DOMAINS.get(intent.action, set())
    # COMPLAINT / SMALL_TALK / UNKNOWN authorize no customer-data domain (default-deny).
    if include_history and has_info:
        allowed |= {DataDomain.INTERACTION_HISTORY}
    return allowed


def allowed_domains(intents: list[Intent], include_history: bool = False) -> list[str]:
    """String form for storage / pre-filtering."""
    return sorted(str(d) for d in domains_for_intents(intents, include_history))
