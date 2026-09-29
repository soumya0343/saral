"""R2: a run interrupted mid-graph resumes from its Postgres checkpoint in a NEW checkpointer
(i.e. another worker process) instead of restarting — earlier nodes are not re-executed.

Needs a migrated Postgres (SARAL_TEST_DATABASE_URL); skipped otherwise.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SARAL_TEST_DATABASE_URL"),
    reason="needs a migrated Postgres (SARAL_TEST_DATABASE_URL)",
)


class _WorkerKilled(BaseException):
    """Stands in for process death: not an Exception, so the runner can't handle it."""


async def test_crashed_run_resumes_from_postgres_checkpoint(monkeypatch):
    from saral.config import get_settings
    from saral.graph import build
    from saral.graph.checkpoint import open_checkpointer
    from saral.schemas import RunRequest
    from saral.worker import runner

    monkeypatch.setattr(get_settings(), "checkpoint_backend", "postgres")
    monkeypatch.setattr(get_settings(), "database_url", os.environ["SARAL_TEST_DATABASE_URL"])

    calls = {"triage": 0, "synthesis": 0}
    real_triage, real_synth = build.triage_node, build.synthesis_node

    async def counting_triage(state):
        calls["triage"] += 1
        return await real_triage(state)

    async def dies_once_synthesis(state):
        calls["synthesis"] += 1
        if calls["synthesis"] == 1:
            raise _WorkerKilled("worker killed mid-synthesis")
        return await real_synth(state)

    async def no_publish(ev):
        return None

    monkeypatch.setattr(build, "triage_node", counting_triage)
    monkeypatch.setattr(build, "synthesis_node", dies_once_synthesis)
    monkeypatch.setattr(runner, "publish_trace", no_publish)

    req = RunRequest(
        run_id=uuid.uuid4().hex,
        conversation_id=uuid.uuid4().hex,
        user_id="U1001",
        message="What does my health policy cover?",
    )
    async with open_checkpointer() as saver:  # worker process A
        assert type(saver).__name__ == "AsyncPostgresSaver"
        with pytest.raises(_WorkerKilled):
            await runner.execute_run(req)

    async with open_checkpointer():  # worker process B (fresh pool, same database)
        second = await runner.execute_run(req)
    assert second.status == "resolved"
    assert second.final_response is not None and second.final_response.message
    assert calls == {"triage": 1, "synthesis": 2}  # resumed at synthesis, triage not redone

    # A finished run's checkpoint (full state incl. the raw message) is deleted.
    async with open_checkpointer() as saver:
        assert await saver.aget_tuple({"configurable": {"thread_id": req.run_id}}) is None


async def test_handled_failure_leaves_no_checkpoint(monkeypatch):
    from saral.config import get_settings
    from saral.graph import build
    from saral.graph.checkpoint import open_checkpointer
    from saral.schemas import RunRequest
    from saral.worker import runner

    monkeypatch.setattr(get_settings(), "checkpoint_backend", "postgres")
    monkeypatch.setattr(get_settings(), "database_url", os.environ["SARAL_TEST_DATABASE_URL"])

    async def broken_synthesis(state):
        raise RuntimeError("bug")

    async def no_publish(ev):
        return None

    monkeypatch.setattr(build, "synthesis_node", broken_synthesis)
    monkeypatch.setattr(runner, "publish_trace", no_publish)
    req = RunRequest(run_id=uuid.uuid4().hex, conversation_id="c", user_id="U1001",
                     message="What does my health policy cover?")
    async with open_checkpointer() as saver:
        state = await runner.execute_run(req)
        assert state.status == "crashed"  # handled: acked, never resumed...
        assert await saver.aget_tuple({"configurable": {"thread_id": req.run_id}}) is None
