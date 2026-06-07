"""Right-to-erasure (DPDP): tombstone a customer's PII-bearing rows.

Erasure tombstones message content and action args/results for a customer while leaving the
hash-chained `audit_log` UNTOUCHED — the audit holds only reason codes + tokenized refs (no raw
PII), so it stays valid (`verify_chain` still passes) and the 7-year regulatory hold and
right-to-erasure coexist. Withdrawal of consent fires this.
"""

from __future__ import annotations

from sqlalchemy import select, update

from saral.db.models import ActionRecordRow, AgentRun, Conversation, Message
from saral.db.session import get_sessionmaker
from saral.logging import get_logger

log = get_logger(__name__)

_TOMBSTONE = "[erased]"


async def erase_user_data(user_id: str) -> dict[str, int]:
    """Tombstone PII for a customer across their conversations. Returns per-table counts."""
    sm = get_sessionmaker()
    async with sm() as session:
        convo_ids = (
            await session.scalars(
                select(Conversation.id).where(Conversation.user_id == user_id)
            )
        ).all()
        if not convo_ids:
            return {"messages": 0, "action_records": 0}

        msgs = await session.execute(
            update(Message)
            .where(Message.conversation_id.in_(convo_ids))
            .values(content=_TOMBSTONE)
        )
        run_ids = (
            await session.scalars(
                select(AgentRun.id).where(AgentRun.conversation_id.in_(convo_ids))
            )
        ).all()
        acts = 0
        if run_ids:
            res = await session.execute(
                update(ActionRecordRow)
                .where(ActionRecordRow.run_id.in_(run_ids))
                .values(args={}, result=None)
            )
            acts = int(getattr(res, "rowcount", 0) or 0)
        # Mark consent withdrawn + clear any suspend custody (which may hold the message).
        await session.execute(
            update(Conversation)
            .where(Conversation.id.in_(convo_ids))
            .values(consent_status="withdrawn", pending_write=None, original_message=None)
        )
        await session.commit()
        counts = {"messages": int(getattr(msgs, "rowcount", 0) or 0), "action_records": acts}
    log.info("erasure.complete", user_ref="(hashed elsewhere)", **counts)
    return counts
