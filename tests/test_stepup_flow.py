"""Full step-up → write-confirmation → idempotent execute flow at the graph level (FR-14/15/19).

Simulates what the API/worker do across runs: a write suspends for step-up (OTP), the verified
customer is re-minted at `step_up`, the parsed value is read back, and only an explicit "yes"
fires the idempotent write. Re-running the confirmed turn must NOT double-write (NFR-7).
"""

from saral.auth import mint_session_token, verify_step_up
from saral.graph.build import build_graph
from saral.graph.state import RunState
from saral.schemas import AuthLevel, PendingWrite
from saral.tools import mock_backend as mb

ORIG = "update my mobile number to 9000000000"


def setup_function():
    mb._reset_state()


async def _run(**kw) -> RunState:
    init = RunState(run_id=kw.pop("run_id", "r"), conversation_id="c", user_id="U1001", **kw)
    return RunState.model_validate(await build_graph().ainvoke(init))


async def test_full_stepup_confirm_execute_no_double_write():
    # Turn 1 — session level: write detected → suspend for step-up (OTP).
    t1 = await _run(raw_message=ORIG, auth_level=AuthLevel.SESSION)
    assert t1.status == "awaiting_input"
    assert t1.step_up_owed
    assert t1.challenge_id
    assert t1.pending_write and t1.pending_write.tool == "update_contact"
    assert t1.pending_write.value == "9000000000"
    pw: PendingWrite = t1.pending_write
    # The OTP is delivered out-of-band (simulated SMS) on the state, not in the message text.
    otp = t1.challenge_otp
    assert otp and "test code" not in t1.final_response.message

    # The customer echoes the OTP — verify_step_up raises auth level (API mints step_up token).
    assert verify_step_up(t1.challenge_id, otp, "U1001")
    _ = mint_session_token("U1001", auth_level=AuthLevel.STEP_UP)

    # Turn 2 — step_up level, pending write carried, no yes/no yet → read-back for confirmation.
    t2 = await _run(
        run_id="r2",
        raw_message=ORIG,
        auth_level=AuthLevel.STEP_UP,
        pending_write=pw,
        intent_nonce=pw.intent_nonce,
    )
    assert t2.status == "awaiting_confirmation"
    assert "9000000000" in t2.final_response.message  # the parsed value is read back
    assert not any(a.ok for a in t2.actions)  # still not executed

    # Turn 3 — explicit "yes" → idempotent write fires, resolved.
    t3 = await _run(
        run_id="r3",
        raw_message=ORIG,
        auth_level=AuthLevel.STEP_UP,
        pending_write=pw,
        intent_nonce=pw.intent_nonce,
        resume_reply="yes",
    )
    assert t3.status == "resolved"
    assert any(a.tool == "update_contact" and a.ok for a in t3.actions)

    # Re-run the confirmed turn (simulates a crash-resume) → same idempotency key, no double write.
    t3b = await _run(
        run_id="r3b",
        raw_message=ORIG,
        auth_level=AuthLevel.STEP_UP,
        pending_write=pw,
        intent_nonce=pw.intent_nonce,
        resume_reply="yes",
    )
    rec = next(a for a in t3b.actions if a.tool == "update_contact")
    assert rec.ok
    assert rec.idempotency_key == pw.idempotency_key  # exactly-once key reused (NFR-7)


async def test_stepup_failure_escalates():
    pw = PendingWrite(
        tool="update_contact",
        field="mobile",
        value="9000000000",
        args={"field": "mobile", "value": "9000000000"},
        read_back="confirm?",
        idempotency_key="k",
        intent_nonce=1,
    )
    # auth still session + a (wrong) challenge_response present → step-up failed → escalate.
    state = await _run(
        raw_message=ORIG,
        auth_level=AuthLevel.SESSION,
        pending_write=pw,
        intent_nonce=1,
        challenge_response="000000",
    )
    assert state.status == "escalated"
    assert state.escalation is not None
    assert state.escalation.blocking_reason == "step_up_failed"


async def test_confirmation_no_abandons_write():
    pw = PendingWrite(
        tool="update_contact",
        field="mobile",
        value="9000000000",
        args={"field": "mobile", "value": "9000000000"},
        read_back="confirm?",
        idempotency_key="k",
        intent_nonce=1,
    )
    state = await _run(
        raw_message=ORIG,
        auth_level=AuthLevel.STEP_UP,
        pending_write=pw,
        intent_nonce=1,
        resume_reply="no",
    )
    assert state.status == "resolved"
    assert state.pending_write is None  # abandoned
    assert not any(a.ok for a in state.actions)
