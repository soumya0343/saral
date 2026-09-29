"""R1: a failed persist is retried, and a final failure is reported to the client, not
swallowed (the reply was shown but a pending confirmation would otherwise vanish)."""

from __future__ import annotations

import pytest

from saral.graph.state import RunState


@pytest.fixture
def runner(monkeypatch):
    from saral.config import get_settings
    from saral.worker import runner as r

    monkeypatch.setattr(get_settings(), "app_env", "dev")  # test env skips persistence
    monkeypatch.setattr(r, "_PERSIST_BACKOFF_S", (0, 0, 0))
    published: list = []

    async def fake_publish(ev):
        published.append(ev)

    monkeypatch.setattr(r, "publish_trace", fake_publish)
    r.published = published  # type: ignore[attr-defined]
    return r


def _state() -> RunState:
    return RunState(run_id="r1", conversation_id="c1", user_id="U1001", raw_message="hi")


async def test_transient_failure_is_retried(runner, monkeypatch):
    from saral.db import repository

    calls = {"n": 0}

    async def flaky(state):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("db blip")

    async def not_persisted(run_id):
        return False

    monkeypatch.setattr(repository, "persist_run", flaky)
    monkeypatch.setattr(repository, "run_persisted", not_persisted)
    await runner._persist_or_warn(_state())
    assert calls["n"] == 3
    assert runner.published == []


async def test_final_failure_warns_the_client(runner, monkeypatch):
    from saral.db import repository

    async def down(state):
        raise ConnectionError("db down")

    async def not_persisted(run_id):
        return False

    monkeypatch.setattr(repository, "persist_run", down)
    monkeypatch.setattr(repository, "run_persisted", not_persisted)
    await runner._persist_or_warn(_state())
    assert [e.type for e in runner.published] == ["warning"]
    assert runner.published[0].data == {"warning": "persist_failed"}


async def test_lost_ack_is_not_retried_twice(runner, monkeypatch):
    from saral.db import repository

    calls = {"n": 0}

    async def commit_then_error(state):
        calls["n"] += 1
        raise ConnectionError("ack lost after commit")

    async def persisted(run_id):
        return True

    monkeypatch.setattr(repository, "persist_run", commit_then_error)
    monkeypatch.setattr(repository, "run_persisted", persisted)
    await runner._persist_or_warn(_state())
    assert calls["n"] == 1 and runner.published == []
