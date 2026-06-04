"""Compliance gate.

Runs before any state-changing action and before any response is sent (TRD §11.2). It is
sequential, deterministic, and fails closed: ambiguity becomes block + escalate rather than
allow. Emits a ComplianceDecision per check, each logged to the audit trail.
"""

from __future__ import annotations

from saral.compliance.authz import check_authorization
from saral.compliance.injection import detect_injection
from saral.schemas import ComplianceDecision, Intent, IntentType


class ComplianceGate:
    name = "compliance"

    def evaluate(
        self, message: str, intents: list[Intent], user_id: str, entities: dict
    ) -> list[ComplianceDecision]:
        decisions: list[ComplianceDecision] = []

        # 1) Prompt-injection — checked on every request; fail closed.
        is_injection, reason = detect_injection(message)
        if is_injection:
            decisions.append(
                ComplianceDecision(decision="block", reason=reason, actor="injection_guard")
            )
            # A detected injection blocks the whole run; no point authorizing actions.
            return decisions

        # 2) Authorization — per state-affecting / data-exposing action.
        action_intents = [i for i in intents if i.type == IntentType.ACTION and i.action]
        for intent in action_intents:
            allowed, why = check_authorization(user_id, intent.action, entities)
            decisions.append(
                ComplianceDecision(
                    decision="allow" if allowed else "block",
                    reason=why,
                    actor="authz",
                    action=intent.action,
                )
            )

        if not decisions:
            decisions.append(
                ComplianceDecision(decision="allow", reason="no gated actions", actor="authz")
            )
        return decisions

    @staticmethod
    def allowed_actions(decisions: list[ComplianceDecision]) -> set[str]:
        """Actions explicitly allowed and not globally blocked by injection."""
        if any(d.actor == "injection_guard" and d.decision == "block" for d in decisions):
            return set()
        return {d.action for d in decisions if d.decision == "allow" and d.action}
