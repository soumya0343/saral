"""Persistence races (R1): the API's user turn and the worker's persist of the assistant turn
must never collide on message sequence numbers, and a worker persist must never be lost.

Needs a migrated Postgres (SARAL_TEST_DATABASE_URL); skipped otherwise.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SARAL_TEST_DATABASE_URL"),
    reason="needs a migrated Postgres (SARAL_TEST_DATABASE_URL)",
)


@pytest.fixture
async def sm():
    from saral.db import session as dbs

    dbs.get_engine.cache_clear()
    dbs.get_sessionmaker.cache_clear()
    yield dbs.get_sessionmaker()
    await dbs.get_engine().dispose()
    dbs.get_engine.cache_clear()
    dbs.get_sessionmaker.cache_clear()


async def _new_conversation(sm) -> str:
    from saral.db.models import Conversation

    async with sm() as s:
        c = Conversation(user_id="U1001", tenant_id="t_demo")
        s.add(c)
        await s.commit()
        return c.id


async def test_concurrent_user_and_assistant_turns_get_unique_seqs(sm):
    from sqlalchemy import select

    from saral.db.models import Message
    from saral.db.repository import allocate_seq, persist_run
    from saral.graph.state import RunState
    from saral.schemas import ResponsePayload

    cid = await _new_conversation(sm)

    async def user_turn(i: int) -> None:  # what routes._append_message does
        async with sm() as s:
            seq = await allocate_seq(s, cid)
            s.add(Message(tenant_id="t_demo", conversation_id=cid, role="user",
                          content=f"u{i}", sequence_num=seq))
            await s.commit()

    async def assistant_turn(i: int) -> None:  # what the worker's persist does
        await persist_run(RunState(
            run_id=uuid.uuid4().hex, conversation_id=cid, user_id="U1001",
            raw_message=f"u{i}", status="resolved",
            final_response=ResponsePayload(resolution_status="resolved", message=f"a{i}"),
        ))

    await asyncio.gather(*(f(i) for i in range(15) for f in (user_turn, assistant_turn)))

    async with sm() as s:
        seqs = (await s.scalars(
            select(Message.sequence_num).where(Message.conversation_id == cid)
        )).all()
    assert sorted(seqs) == list(range(1, 31))  # 30 turns, no gaps, no duplicates, none lost
