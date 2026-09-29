"""Drive an eval scenario through the real graph, turn by turn, like the API + worker would.

Between turns it keeps exactly what the conversation row keeps (see api/routes.py and
db/repository.persist_run): redacted history, suspend custody (status, pending write, OTP
challenge, original message), the step-up grant, and the sticky language. A reply to a suspended
turn goes through the SAME `plan_reply` the API uses, so the eval covers the real resume logic.
"""

from __future__ import annotations

import time

from saral.actions.turns import SuspendState, plan_reply
from saral.compliance.pii import redact_pii
from saral.config import get_settings
from saral.eval.metrics import Outcome, check_expected
from saral.eval.schemas import Scenario
from saral.graph.build import build_graph
from saral.graph.state import RunState
from saral.schemas import AuthLevel, HistoryTurn, Language
from saral.tools import mock_backend as mb

_SUSPENDED = ("awaiting_input", "awaiting_confirmation")


class ConversationDriver:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.cid = f"eval-{scenario.id}"
        self.turn_no = 0
        self.history: list[HistoryTurn] = []
        self.suspend: SuspendState | None = None
        self.grant_key: str | None = None
        self.grant_until = 0.0
        self.language: str | None = None
        self.last_otp: str | None = None

    def _materialize(self, text: str) -> str:
        if "{otp}" in text:
            text = text.replace("{otp}", self.last_otp or "000000")
        if "{wrong_otp}" in text:
            wrong = "000000" if self.last_otp != "000000" else "111111"
            text = text.replace("{wrong_otp}", wrong)
        return text

    async def say(self, raw: str) -> tuple[str, RunState]:
        """One customer message -> the run's final state. Returns (text actually sent, state)."""
        text = self._materialize(raw)
        self.turn_no += 1
        common = {
            "run_id": f"{self.cid}-t{self.turn_no}",
            "conversation_id": self.cid,
            "user_id": self.scenario.user_id,
            "history": list(self.history),
            "known_entities": mb.get_customer_context(self.scenario.user_id),
            "language_hint": Language(self.language) if self.language else None,
        }
        if self.suspend is not None:
            pending = self.suspend.pending_write
            self.suspend.step_up_granted = (
                pending is not None
                and self.grant_key == pending.idempotency_key
                and time.time() < self.grant_until
            )
            plan = await plan_reply(self.suspend, text, self.scenario.user_id, self.history)
            if plan.grant_step_up and pending is not None:
                self.grant_key = pending.idempotency_key
                self.grant_until = time.time() + get_settings().step_up_grant_ttl_s
            init = RunState(
                **common,
                raw_message=plan.message,
                auth_level=plan.auth_level,
                pending_write=plan.pending_write,
                intent_nonce=plan.intent_nonce,
                resume_reply=plan.resume_reply,
                challenge_response=plan.challenge_response,
                challenge_id=plan.challenge_id,
                otp_attempts_left=plan.otp_attempts_left,
                reply_text=text,
            )
        else:
            init = RunState(**common, raw_message=text, auth_level=AuthLevel.SESSION)

        state = RunState.model_validate(await build_graph().ainvoke(init))
        self._after_run(text, state)
        return text, state

    def _after_run(self, text: str, state: RunState) -> None:
        """What the API + persist_run store between turns."""
        self.history.append(HistoryTurn(role="user", content=redact_pii(text)))
        if state.final_response and state.final_response.message:
            reply = redact_pii(state.final_response.message)
            self.history.append(HistoryTurn(role="assistant", content=reply))
        if state.language:
            self.language = str(state.language)
        self.last_otp = state.challenge_otp or self.last_otp
        if state.status in _SUSPENDED:
            self.suspend = SuspendState(
                suspend_status=str(state.status),
                pending_write=state.pending_write,
                challenge_id=state.challenge_id,
                original_message=state.raw_message,
                step_up_granted=False,
            )
        else:  # terminal: custody cleared, step-up grant consumed
            self.suspend = None
            self.grant_key = None


async def run_scenario(scenario: Scenario) -> Outcome:
    """Single-turn: the message (plus, for a write with complete_stepup, a verified "yes").
    Multi-turn: every turn, recording which turns missed their expectations."""
    if scenario.is_multi_turn:
        driver = ConversationDriver(scenario)
        states: list[RunState] = []
        sent: list[str] = []
        failed: list[int] = []
        for i, turn in enumerate(scenario.turns):
            text, state = await driver.say(turn.say)
            sent.append(text)
            states.append(state)
            if check_expected(turn.expect, state):
                failed.append(i)
        return Outcome(states=states, user_turns=sent, failed_turns=failed)

    graph = build_graph()
    init = RunState(
        run_id=f"eval-{scenario.id}",
        conversation_id=f"eval-{scenario.id}",
        user_id=scenario.user_id,
        auth_level=AuthLevel(scenario.auth_level),
        raw_message=scenario.message,
        # Like the API: the customer's own ids, so "my claim" resolves without an id.
        known_entities=mb.get_customer_context(scenario.user_id),
    )
    state = RunState.model_validate(await graph.ainvoke(init))
    states, sent = [state], [scenario.message]
    # Drive a suspended write through step-up + confirmation (a verified, confirming customer),
    # so the eval measures the full resolution path and tool sequence.
    if (
        scenario.complete_stepup
        and state.pending_write is not None
        and state.status in _SUSPENDED
    ):
        resume = RunState(
            run_id=f"eval-{scenario.id}-confirm",
            conversation_id=f"eval-{scenario.id}",
            user_id=scenario.user_id,
            auth_level=AuthLevel.STEP_UP,  # the verified customer
            raw_message=scenario.message,
            known_entities=mb.get_customer_context(scenario.user_id),
            pending_write=state.pending_write,
            intent_nonce=state.pending_write.intent_nonce,
            resume_reply="yes",
        )
        states.append(RunState.model_validate(await graph.ainvoke(resume)))
        sent.append("yes")
    return Outcome(states=states, user_turns=sent)
