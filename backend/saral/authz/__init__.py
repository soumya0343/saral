"""Authorization policy — the deterministic boundaries the LLM never controls.

Holds the intent→domain purpose-limitation map (DPDP) and the per-action step-up risk tier.
"""

from saral.authz.policy import (
    DataDomain,
    allowed_domains,
    domains_for_intents,
    requires_step_up,
)

__all__ = [
    "DataDomain",
    "allowed_domains",
    "domains_for_intents",
    "requires_step_up",
]
