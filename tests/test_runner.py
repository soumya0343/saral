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
        message="मेरे क्लेम का स्टेटस क्या है",
    )
    state = await runner.execute_run(req)

    assert state.status == "resolved"
    assert state.language == "hi"

    types = [e.type for e in captured]
    assert types[0] == "run_started"
    assert "intent" in types
    assert "final" in types
    assert types[-1] == "run_finished"

    intent_ev = next(e for e in captured if e.type == "intent")
    actions = [i.get("action") for i in intent_ev.data["intents"]]
    assert "get_claim_status" in actions
