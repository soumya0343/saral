"""Pending-write TTL: a stale write is abandoned, never auto-fired (ADR-0004 'authority is mortal').

Two guarantees:
  - On resume, a pending write past its TTL is discarded so the run re-earns step-up + confirm.
  - The orphan reaper abandons stale suspended conversations and records the close + escalation.
"""

import time

from saral.actions.confirm import build_pending_write, is_stale
from saral.config import get_settings
from saral.graph.build import build_graph
from saral.graph.state import RunState
from saral.schemas import AuthLevel, Intent, IntentType
from saral.tools import mock_backend as mb

ORIG = "update my mobile number to 9000000000"


def setup_function():
    mb._reset_state()


def test_is_stale_respects_ttl():
    intent = Intent(type=IntentType.ACTION, action="update_contact", confidence=0.9)
    pw = build_pending_write("c", intent, {"mobile": "9000000000"}, ORIG, 1)
    assert pw is not None and pw.created_at is not None
    assert not is_stale(pw)  # fresh
    stale_ts = time.time() - get_settings().pending_write_ttl_s - 10
    old = pw.model_copy(update={"created_at": stale_ts})
    assert is_stale(old)  # past TTL


async def test_stale_pending_write_re_earns_stepup():
    # A confirmed-looking resume ("yes") carrying a STALE pending write must NOT execute the
    # write; it must rebuild and re-earn step-up (suspend again), never auto-fire.
    intent = Intent(type=IntentType.ACTION, action="update_contact", confidence=0.9)
    stale = build_pending_write("c", intent, {"mobile": "9000000000"}, ORIG, 1).model_copy(
        update={"created_at": time.time() - get_settings().pending_write_ttl_s - 10}
    )
    init = RunState(
        run_id="r2",
        conversation_id="c",
        user_id="U1001",
        auth_level=AuthLevel.SESSION,
        raw_message=ORIG,
        pending_write=stale,
        intent_nonce=stale.intent_nonce,
        resume_reply="yes",
    )
    out = RunState.model_validate(await build_graph().ainvoke(init))
    # Re-earns step-up: suspends for OTP again instead of executing the stale write.
    assert out.status == "awaiting_input"
    assert out.step_up_owed
    assert not any(a.tool == "update_contact" and a.ok for a in out.actions)
