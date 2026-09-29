"""How a customer's reply to a suspended conversation becomes the next run.

Shared by the API (`POST /conversations/{id}/reply`) and the multi-turn eval driver, so the eval
exercises exactly the resume logic customers hit — not a re-implementation of it.

Inputs are what the conversation row holds while suspended (`SuspendState`); the output is a
`ReplyPlan`: how to fill the next `RunRequest`, and whether a step-up grant was just earned.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from saral.actions.resume import interpret_resume
from saral.auth.stepup import attempts_left, verify_step_up
from saral.schemas import AuthLevel, HistoryTurn, PendingWrite


@dataclass
class SuspendState:
    suspend_status: str  # awaiting_input | awaiting_confirmation
    pending_write: PendingWrite | None
    challenge_id: str | None
    original_message: str | None
    step_up_granted: bool  # a live grant exists for THIS pending write


@dataclass
class ReplyPlan:
    message: str  # what the next run processes (the original, or the reply on a topic switch)
    auth_level: AuthLevel
    pending_write: PendingWrite | None
    intent_nonce: int
    resume_reply: str | None = None
    challenge_response: str | None = None
    challenge_id: str | None = None
    otp_attempts_left: int = 0
    # The OTP was verified this turn: the caller persists a grant for pending_write's key.
    grant_step_up: bool = False


async def plan_reply(
    s: SuspendState, reply: str, user_id: str, history: list[HistoryTurn]
) -> ReplyPlan:
    pending = s.pending_write
    original = s.original_message or reply
    plan = ReplyPlan(
        message=original,  # re-send the original so the write re-evaluates (not the reply)
        auth_level=AuthLevel.STEP_UP if s.step_up_granted else AuthLevel.SESSION,
        pending_write=pending,
        intent_nonce=pending.intent_nonce if pending else 0,
    )

    if s.suspend_status == "awaiting_input" and s.challenge_id:
        # Step-up OTP reply. A one-time code is a security credential — verified deterministically,
        # never interpreted by an LLM. Success grants step-up for this pending write, briefly.
        if await asyncio.to_thread(verify_step_up, s.challenge_id, reply, user_id):
            plan.grant_step_up = pending is not None
            plan.auth_level = AuthLevel.STEP_UP
        else:
            # Wrong code: the graph re-prompts while tries remain, escalates once burned.
            plan.challenge_response = reply
            plan.challenge_id = s.challenge_id
            plan.otp_attempts_left = await asyncio.to_thread(attempts_left, s.challenge_id)
        return plan

    if (
        s.suspend_status == "awaiting_input"
        and pending is not None
        and pending.tool == "file_claim"
        and pending.args.get("missing")
    ):
        # Claim intake: the reply fills slots (identity merges them), cancels, or is a new topic.
        await _claim_reply(plan, reply, user_id, pending)
        return plan

    # An LLM interprets the reply (confirm / reject / unclear / value / correction / other) from
    # the suspend context + history, under a deterministic AND-guard. The write trigger stays
    # deterministic — "confirm" is normalized to the canonical "yes" and identity_node re-parses
    # yes/no before acting.
    verdict = await interpret_resume(s.suspend_status, pending, original, reply, history)
    if s.suspend_status == "awaiting_confirmation":
        if verdict.kind == "confirm":
            plan.resume_reply = "yes"
        elif verdict.kind == "reject":
            plan.resume_reply = "no"  # identity abandons the pending write (cancelled)
        elif verdict.kind == "unclear":
            # Mixed yes/no ("haan nahi"): keep the pending write; identity re-parses this reply
            # as unclear and re-asks the read-back instead of acting on it.
            plan.resume_reply = reply
        else:  # value / correction / other → topic switch: drop pending, run fresh
            _topic_switch(plan, reply)
    elif verdict.kind == "value":  # awaiting_input clarification (missing contact value)
        # Fold the value into the original so triage/identity re-extract (or flag invalid).
        plan.message = f"{original} {reply}".strip()
    else:  # correction / reject / other → run fresh; triage + history resolve it
        _topic_switch(plan, reply)
    return plan


async def _claim_reply(plan: ReplyPlan, reply: str, user_id: str, pending: PendingWrite) -> None:
    from saral.actions import claim_intake
    from saral.actions.confirm import parse_confirmation
    from saral.agents.triage import classify_intents
    from saral.schemas import IntentType
    from saral.tools import mock_backend as mb

    ctx = await asyncio.to_thread(mb.get_customer_context, user_id)
    policies = [str(p) for p in ctx.get("policy_ids") or []]
    missing = set(pending.args.get("missing") or [])
    found = claim_intake.extract(reply, missing=missing, policies=policies)
    if not found:
        found = await claim_intake.llm_extract(reply, missing)
    # Free text only counts as the description when it isn't itself a new request/question.
    asks = {IntentType.ACTION, IntentType.INFORMATION}
    if set(found) == {"description"} and (
        reply.strip().endswith("?") or any(i.type in asks for i in classify_intents(reply))
    ):
        found = {}
    if found:
        plan.resume_reply = reply  # identity merges these slots into the pending claim
    elif parse_confirmation(reply) == "no":
        plan.resume_reply = "no"  # identity abandons the claim (cancelled)
    else:
        _topic_switch(plan, reply)


def _topic_switch(plan: ReplyPlan, reply: str) -> None:
    plan.message = reply
    plan.pending_write = None
    plan.intent_nonce = 0
    plan.auth_level = AuthLevel.SESSION  # a grant is bound to the dropped write
