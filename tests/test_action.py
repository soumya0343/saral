import pytest

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


async def test_update_contact_uses_idempotency(agent):
    intents = [Intent(type=IntentType.ACTION, action="update_contact")]
    recs = await agent.run(intents, {"mobile": "9000000000"}, "U1001", "runX", "update number")
    assert recs[0].ok
    assert recs[0].idempotency_key == "runX:update_contact"
    assert recs[0].result["changed"]["new"] == "9000000000"


async def test_update_contact_missing_value_asks(agent):
    intents = [Intent(type=IntentType.ACTION, action="update_contact")]
    recs = await agent.run(intents, {}, "U1001", "run1", "update my number")
    assert recs[0].needs_clarification
