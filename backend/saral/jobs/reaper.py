"""Orphan reaper: abandon suspended conversations whose pending write has out-lived its TTL.

A confirmation/clarification suspends the run and persists a `pending_write`; if the customer
never replies, that write's execution authority must expire ("authority is mortal").
This job scans suspended conversations, and for any whose pending write is past its TTL:
  - clears the suspend custody and marks the conversation `abandoned`,
  - records a terminal `orphaned` AgentRun close (: abnormal close, not data-loss),
  - emits an `orphan_ttl` Escalation so a human can follow up.
A stale write is NEVER auto-fired; if the customer returns, resume re-earns step-up + confirm.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import select

from saral.config import get_settings
from saral.db.models import AgentRun, Conversation, Escalation
from saral.db.session import get_sessionmaker
from saral.logging import get_logger

log = get_logger(__name__)

_SUSPENDED = ("awaiting_input", "awaiting_confirmation")


def _is_stale(pending_write: dict | None, now: float, ttl: int) -> bool:
    if not pending_write:
        return False
    created = pending_write.get("created_at")
    if created is None:
        return False
    return now - created > ttl


async def reap_orphans(now: float | None = None) -> int:
    """Abandon stale suspended conversations. Returns the count reaped."""
    settings = get_settings()
    ttl = settings.orphan_ttl_s
    now = now if now is not None else time.time()
    reaped = 0
    sm = get_sessionmaker()
    async with sm() as session:
        rows = (
            await session.scalars(
                select(Conversation).where(Conversation.suspend_status.in_(_SUSPENDED))
            )
        ).all()
        for convo in rows:
            if not _is_stale(convo.pending_write, now, ttl):
                continue
            pending_tool = (convo.pending_write or {}).get("tool")
            # Abnormal terminal close for the orphaned run.
            session.add(
                AgentRun(
                    tenant_id=convo.tenant_id,
                    conversation_id=convo.id,
                    status="orphaned",
                    close_reason="orphaned",
                    closed_at=datetime.now(UTC),
                    step_count=0,
                )
            )
            session.add(
                Escalation(
                    tenant_id=convo.tenant_id,
                    conversation_id=convo.id,
                    detected_intent=pending_tool or "write",
                    blocking_reason="orphan_ttl",
                    pending_action=pending_tool,
                    transcript_ref=convo.id,
                    sla_target="24h",
                )
            )
            # Clear custody — the pending write is dead; resume must re-earn authority.
            convo.suspend_status = None
            convo.pending_write = None
            convo.challenge_id = None
            convo.original_message = None
            convo.status = "abandoned"
            reaped += 1
        if reaped:
            await session.commit()
    if reaped:
        log.info("reaper.orphans_abandoned", count=reaped)
    return reaped
