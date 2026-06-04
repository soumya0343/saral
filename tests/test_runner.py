"""End-to-end run through the triage graph, capturing trace events without Redis."""

import pytest

from saral.schemas import RunRequest, TraceEvent
from saral.worker import runner


@pytest.fixture
def captured(monkeypatch):
    events: list[TraceEvent] = []

    async def fake_publish(event: TraceEvent) -> None:
        events.append(event)

    monkeypatch.setattr(runner, "publish_trace", fake_publish)
    return events


async def test_execute_run_emits_trace_and_resolves(captured):
    req = RunRequest(
        run_id="r1",
        conversation_id="c1",
        user_id="U1001",
        message="क्लेम CLM2001 का स्टेटस क्या है",
    )
    state = await runner.execute_run(req)

    assert state.status == "resolved"
    assert state.language == "hi"
    assert state.route == "action"

    types = [e.type for e in captured]
    assert types[0] == "run_started"
    assert "intent" in types
    assert "route" in types
    assert "action" in types
    assert "final" in types
    assert types[-1] == "run_finished"

    intent_ev = next(e for e in captured if e.type == "intent")
    actions = [i.get("action") for i in intent_ev.data["intents"]]
    assert "get_claim_status" in actions

    action_ev = next(e for e in captured if e.type == "action")
    assert action_ev.data["actions"][0]["ok"] is True


async def test_information_query_routes_to_rag(captured):
    req = RunRequest(
        run_id="r2",
        conversation_id="c2",
        user_id="U1001",
        message="What does my health policy cover?",
    )
    state = await runner.execute_run(req)
    assert state.route == "rag"
    assert state.retrieved
    assert "retrieval" in [e.type for e in captured]
