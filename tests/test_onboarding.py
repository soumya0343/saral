"""Customer onboarding: login-or-create, and 'my claim' resolution via known entities."""

import pytest

from saral.graph.build import build_graph
from saral.graph.state import RunState
from saral.tools import mock_backend as mb
from saral.tools.mock_backend import ToolError


def setup_function():
    mb._reset_state()


def test_new_customer_is_provisioned():
    c = mb.identify_customer("Priya Nair", mobile="9123456789")
    assert c["returning"] is False
    assert c["user_id"].startswith("U")
    assert c["policy_id"] and c["claim_id"]
    # The provisioned policy + claim are owned by the new customer.
    assert mb.get_policy_details(c["policy_id"]).holder_user_id == c["user_id"]
    assert mb.get_claim_status(c["claim_id"]).policy_id == c["policy_id"]


def test_returning_customer_matched_by_mobile():
    c = mb.identify_customer("Asha Verma", mobile="9876500001")  # seeded U1001
    assert c["returning"] is True
    assert c["user_id"] == "U1001"


def test_returning_customer_matched_by_email_case_insensitive():
    c = mb.identify_customer("Asha", email="ASHA@example.com")
    assert c["user_id"] == "U1001"


def test_requires_name_and_contact():
    with pytest.raises(ToolError):
        mb.identify_customer("", mobile="9123456789")
    with pytest.raises(ToolError):
        mb.identify_customer("No Contact")


def test_customer_context_returns_owned_ids():
    ctx = mb.get_customer_context("U1001")
    assert ctx["policy_id"] == "POL1001"
    assert ctx["claim_id"] == "CLM2001"


async def test_my_claim_status_resolves_via_known_entities():
    c = mb.identify_customer("New User", mobile="9555000111")
    ctx = mb.get_customer_context(c["user_id"])
    graph = build_graph()
    init = RunState(
        run_id="r", conversation_id="conv", user_id=c["user_id"],
        raw_message="what is my claim status?", known_entities=ctx,
    )
    state = RunState.model_validate(await graph.ainvoke(init))
    assert state.route == "action"
    assert state.entities.get("claim_id") == c["claim_id"]
    assert any(a.tool == "get_claim_status" and a.ok for a in state.actions)
