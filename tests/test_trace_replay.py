"""R6: trace events are replayable — a client that subscribes AFTER posting (or reconnects)
misses nothing, and only sees its own run. Needs Redis (SARAL_TEST_REDIS_URL)."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SARAL_TEST_REDIS_URL"), reason="needs Redis (SARAL_TEST_REDIS_URL)"
)


@pytest.fixture
def bus(monkeypatch):
    from saral import runbus
    from saral.config import get_settings

    monkeypatch.setattr(get_settings(), "redis_url", os.environ["SARAL_TEST_REDIS_URL"])
    runbus.get_redis.cache_clear()
    yield runbus
    runbus.get_redis.cache_clear()


def _ev(runbus_mod, cid: str, run_id: str, type_: str):
    from saral.schemas import TraceEvent

    return TraceEvent(type=type_, run_id=run_id, conversation_id=cid)


async def _collect(gen) -> list[tuple[str, str]]:
    return [x async for x in gen]


async def test_late_subscriber_replays_its_run_only(bus):
    import json

    cid = uuid.uuid4().hex
    for run, t in [("A", "run_started"), ("B", "run_started"), ("A", "final"),
                   ("B", "final"), ("A", "run_finished"), ("B", "run_finished")]:
        await bus.publish_trace(_ev(bus, cid, run, t))

    # Subscribing after everything was published still delivers run A, in order, then stops.
    got = await asyncio.wait_for(_collect(bus.subscribe_trace(cid, run_id="A")), timeout=5)
    assert [json.loads(p)["type"] for _, p in got] == ["run_started", "final", "run_finished"]
    assert {json.loads(p)["run_id"] for _, p in got} == {"A"}


async def test_reconnect_resumes_after_last_event_id(bus):
    import json

    cid = uuid.uuid4().hex
    for t in ["run_started", "intent", "final", "run_finished"]:
        await bus.publish_trace(_ev(bus, cid, "A", t))
    first_two = []
    async for eid, p in bus.subscribe_trace(cid, run_id="A"):
        first_two.append((eid, p))
        if len(first_two) == 2:
            break  # connection dropped here
    rest = await asyncio.wait_for(
        _collect(bus.subscribe_trace(cid, run_id="A", last_event_id=first_two[-1][0])), timeout=5
    )
    assert [json.loads(p)["type"] for _, p in rest] == ["final", "run_finished"]


async def test_live_events_arrive_while_subscribed(bus):
    import json

    cid = uuid.uuid4().hex

    async def publish_later():
        await asyncio.sleep(0.2)
        for t in ["run_started", "run_finished"]:
            await bus.publish_trace(_ev(bus, cid, "A", t))

    pub = asyncio.create_task(publish_later())
    got = await asyncio.wait_for(_collect(bus.subscribe_trace(cid, run_id="A")), timeout=5)
    await pub
    assert [json.loads(p)["type"] for _, p in got] == ["run_started", "run_finished"]
