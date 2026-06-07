"""Identity gate.

Deterministic node that runs after triage and before any agent reads customer data. It
consumes the validated session claims (already on the RunState — token-derived, never from
the message) and, for action intents, decides whether step-up is owed. It NEVER raises
`auth_level` itself; that only happens via a verified challenge (auth/stepup.py).
"""

from __future__ import annotations

from pydantic import BaseModel

from saral.authz.policy import requires_step_up
from saral.schemas import AuthLevel, Intent, IntentType


class SessionClaims(BaseModel):
    user_id: str
    tenant_id: str
    auth_level: AuthLevel = AuthLevel.SESSION


class StepUpDecision(BaseModel):
    owed: bool = False
    action: str | None = None  # the state-changing action that needs step-up
    reason: str = "no step-up required"


class IdentityGate:
    """Decides whether a request may proceed and whether step-up is owed."""

    name = "identity"

    def evaluate(self, auth_level: AuthLevel, intents: list[Intent]) -> StepUpDecision:
        # Find the first state-changing action that the current auth level cannot satisfy.
        for intent in intents:
            if intent.type != IntentType.ACTION or not intent.action:
                continue
            if requires_step_up(intent.action) and auth_level.rank < AuthLevel.STEP_UP.rank:
                return StepUpDecision(
                    owed=True,
                    action=intent.action,
                    reason=f"action '{intent.action}' requires step-up; have '{auth_level}'",
                )
        return StepUpDecision()


_gate = IdentityGate()


def identity_gate(auth_level: AuthLevel, intents: list[Intent]) -> StepUpDecision:
    return _gate.evaluate(auth_level, intents)
