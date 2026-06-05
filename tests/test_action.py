import pytest

from saral.actions.confirm import build_pending_write, idempotency_key, parse_confirmation
from saral.agents.action import ActionAgent
from saral.schemas import Intent, IntentType
from saral.tools import mock_backend as mb


def setup_function():
    mb._reset_state()


@pytest.fixture
def agent():
    return ActionAgent()


async def test_get_claim_status(agent):
    intents = [Intent(type=IntentType.ACTION, action="get_claim_status")]
    recs = await agent.run(intents, {"claim_id": "CLM2001"}, "U1001", "run1", "status?")
    assert recs[0].ok
    assert recs[0].result["status"] == "under_review"


async def test_missing_claim_id_asks(agent):
    intents = [Intent(type=IntentType.ACTION, action="get_claim_status")]
    recs = await agent.run(intents, {}, "U1001", "run1", "claim status")
    assert recs[0].needs_clarification
    assert not recs[0].ok


async def test_write_executes_only_via_confirmed_pending(agent):
    """A gated write executes off its conversation-anchored PendingWrite, not a fresh intent."""
    intents = [Intent(type=IntentType.ACTION, action="update_contact")]
    pw = build_pending_write("conv1", intents[0], {"mobile": "9000000000"}, "update number", 1)
    assert pw is not None
    recs = await agent.run(
        intents, {"mobile": "9000000000"}, "U1001", "runX", "update number", pending_write=pw
    )
    assert recs[0].ok
    # Idempotency key is conversation-anchored (survives a run-id change on resume), not run-id.
    assert recs[0].idempotency_key == pw.idempotency_key
    assert recs[0].result["changed"]["new"] == "9000000000"


async def test_write_intent_without_pending_does_not_execute(agent):
    """Without a confirmed PendingWrite the write is a no-op (it must be gated first)."""
    intents = [Intent(type=IntentType.ACTION, action="update_contact")]
    recs = await agent.run(intents, {"mobile": "9000000000"}, "U1001", "runX", "update number")
    assert recs == []


def test_build_pending_write_missing_value_returns_none():
    intent = Intent(type=IntentType.ACTION, action="update_contact")
    assert build_pending_write("conv1", intent, {}, "update my number", 1) is None


def test_idempotency_key_is_conversation_anchored_and_stable():
    a = idempotency_key("conv1", "update_contact", {"field": "mobile", "value": "9"}, 1)
    b = idempotency_key("conv1", "update_contact", {"field": "mobile", "value": "9"}, 1)
    c = idempotency_key("conv1", "update_contact", {"field": "mobile", "value": "9"}, 2)
    assert a == b  # deterministic
    assert a != c  # nonce changes the key


def test_parse_confirmation_multilingual():
    assert parse_confirmation("yes") == "yes"
    assert parse_confirmation("haan") == "yes"
    assert parse_confirmation("हाँ") == "yes"
    assert parse_confirmation("no") == "no"
    assert parse_confirmation("nahi") == "no"
    assert parse_confirmation("maybe later") == "unclear"
