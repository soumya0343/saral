"""Compliance gate.

Runs before any state-changing action and before any response is sent. It is
sequential, deterministic, and fails closed: ambiguity becomes block + escalate rather than
allow. Emits a ComplianceDecision per check, each logged to the audit trail.
"""

from __future__ import annotations

from saral.compliance.authz import check_authorization
from saral.compliance.injection import detect_injection
from saral.schemas import ComplianceDecision, Intent, IntentType, ReasonCode


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
                ComplianceDecision(
                    decision="block",
                    reason=reason,
                    actor="injection_guard",
                    reason_code=ReasonCode.INJECTION_DETECTED,
                )
            )
            # A detected injection blocks the whole run; no point authorizing actions.
            return decisions

        # 2) Referenced-id ownership — impersonation defense. A message naming
        # ANOTHER customer's claim/policy id is blocked regardless of intent, so impersonation
        # phrased as an explanation ("why was claim X rejected") can never leak or act.
        _ID_ACTIONS = (("get_claim_status", "claim_id"), ("get_policy_details", "policy_id"))
        for action_name, key in _ID_ACTIONS:
            if entities.get(key):
                allowed, why = check_authorization(user_id, action_name, entities)
                if not allowed:
                    decisions.append(
                        ComplianceDecision(
                            decision="block",
                            reason=why,
                            actor="authz",
                            action=action_name,
                            reason_code=ReasonCode.OWNERSHIP_DENIED,
                        )
                    )
                    return decisions

        # 3) Authorization — per state-affecting / data-exposing action.
        action_intents = [i for i in intents if i.type == IntentType.ACTION and i.action]
        for intent in action_intents:
            allowed, why = check_authorization(user_id, intent.action, entities)
            decisions.append(
                ComplianceDecision(
                    decision="allow" if allowed else "block",
                    reason=why,
                    actor="authz",
                    action=intent.action,
                    reason_code=ReasonCode.AUTHORIZED if allowed else ReasonCode.AUTHZ_DENIED,
                )
            )

        if not decisions:
            decisions.append(
                ComplianceDecision(
                    decision="allow",
                    reason="no gated actions",
                    actor="authz",
                    reason_code=ReasonCode.NO_GATED_ACTIONS,
                )
            )
        return decisions

    @staticmethod
    def allowed_actions(decisions: list[ComplianceDecision]) -> set[str]:
        """Actions explicitly allowed and not globally blocked by injection."""
        if any(d.actor == "injection_guard" and d.decision == "block" for d in decisions):
            return set()
        return {d.action for d in decisions if d.decision == "allow" and d.action}
