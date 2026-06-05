"""Routing accuracy: info vs action queries take different graph paths (Phase 2 exit)."""

from saral.graph.build import build_graph
from saral.graph.state import RunState
from saral.tools import mock_backend as mb


def setup_function():
    mb._reset_state()


async def _run(message: str, **kw) -> RunState:
    graph = build_graph()
    init = RunState(
        run_id="r", conversation_id="c", user_id="U1001", raw_message=message, **kw
    )
    result = await graph.ainvoke(init)
    return RunState.model_validate(result)


async def test_information_query_routes_to_rag():
    state = await _run("What does my health policy cover?")
    assert state.route == "rag"
    assert state.retrieved
    assert not state.actions


async def test_write_action_suspends_for_stepup():
    # A state-changing action at session level must gate on step-up, not execute (FR-14).
    state = await _run("Please update my mobile number to 9000000000")
    assert state.route == "suspend"
    assert state.status == "awaiting_input"
    assert state.pending_write is not None
    assert state.pending_write.tool == "update_contact"
    assert not state.actions  # nothing executed before step-up


async def test_claim_status_routes_to_action_with_entity():
    state = await _run("What is the status of claim CLM2001?")
    assert state.route == "action"
    assert state.actions[0].tool == "get_claim_status"
    assert state.actions[0].ok


async def test_paths_differ():
    info = await _run("What does my policy cover?")
    action = await _run("update my email to a@b.com")
    assert info.route != action.route
