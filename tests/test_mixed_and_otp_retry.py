"""R3 (mixed read + write answers the read too), the confirmed-write guard, and R4 (a wrong
OTP re-prompts with tries left instead of escalating at once)."""

from saral.graph.build import build_graph
from saral.graph.state import RunState
from saral.schemas import AuthLevel
from saral.tools import mock_backend as mb


def setup_function():
    mb._reset_state()


async def _run(**kw) -> RunState:
    init = RunState(run_id=kw.pop("run_id", "r"), conversation_id="c", user_id="U1001", **kw)
    return RunState.model_validate(await build_graph().ainvoke(init))


async def test_mixed_read_and_write_answers_read_then_asks_for_otp():
    t = await _run(
        raw_message="What is the status of claim CLM2001 and update my mobile to 9000000000",
        known_entities={"policy_id": "POL1001"},
    )
    # The read ran and is in the reply...
    assert any(a.tool == "get_claim_status" and a.ok for a in t.actions)
    assert "CLM2001" in t.final_response.message
    # ...and the write is suspended for step-up, not dropped and not executed.
    assert t.status == "awaiting_input"
    assert t.final_response.resolution_status == "awaiting"
    assert t.challenge_id and t.challenge_otp
    assert t.pending_write and t.pending_write.value == "9000000000"
    assert not any(a.tool == "update_contact" for a in t.actions)


async def test_info_plus_write_retrieves_and_suspends():
    # (No "how do I": the capability guard still suppresses writes message-wide — Phase 3.6.)
    t = await _run(
        raw_message="What is the premium grace period? Also update my email to asha@new.in"
    )
    assert t.retrieved  # the information part was answered from retrieval
    assert t.status == "awaiting_input"
    assert t.pending_write and t.pending_write.value == "asha@new.in"


async def test_pure_write_still_suspends_without_answering_anything():
    t = await _run(raw_message="update my mobile to 9000000000")
    assert t.status == "awaiting_input" and t.route == "suspend"
    assert not t.actions and not t.retrieved


async def test_pending_write_never_executes_without_explicit_yes():
    first = await _run(raw_message="update my mobile to 9000000000")
    # A step_up turn that carries the pending write but NO confirmation must not act on it.
    t = await _run(
        run_id="r2",
        raw_message="update my mobile to 9000000000",
        auth_level=AuthLevel.STEP_UP,
        pending_write=first.pending_write,
        intent_nonce=first.pending_write.intent_nonce,
    )
    assert t.status == "awaiting_confirmation"
    assert not t.write_confirmed
    assert not any(a.tool == "update_contact" for a in t.actions)


async def test_wrong_otp_reprompts_while_tries_remain():
    first = await _run(raw_message="update my mobile to 9000000000")
    t = await _run(
        run_id="r2",
        raw_message="update my mobile to 9000000000",
        pending_write=first.pending_write,
        intent_nonce=first.pending_write.intent_nonce,
        challenge_id=first.challenge_id,
        challenge_response="000000",
        otp_attempts_left=3,
    )
    assert t.status == "awaiting_input"  # not escalated
    assert t.escalation is None
    assert "3" in t.final_response.message
    assert t.challenge_id == first.challenge_id  # same challenge; no new code sent


async def test_wrong_otp_escalates_once_challenge_is_burned():
    first = await _run(raw_message="update my mobile to 9000000000")
    t = await _run(
        run_id="r2",
        raw_message="update my mobile to 9000000000",
        pending_write=first.pending_write,
        intent_nonce=first.pending_write.intent_nonce,
        challenge_id=first.challenge_id,
        challenge_response="000000",
        otp_attempts_left=0,
    )
    assert t.status == "escalated"
    assert t.escalation and t.escalation.blocking_reason == "step_up_failed"
