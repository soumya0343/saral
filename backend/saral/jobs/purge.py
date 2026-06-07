"""Data-retention purge (CONTEXT 'Retention'). Run on a schedule (cron / k8s CronJob).

Per-table TTL:
  - redacted messages: deleted after `message_retention_days` (default 90d),
  - dedup idempotency keys: purged at the pending-write TTL (ADR-0004),
  - audit_log: held for `audit_retention_years` (default 7y) — erasure-compatible by design,
    since it holds only reason codes + tokenized refs (no raw PII to erase).

Invoke: `python -m saral.jobs.purge`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from saral.config import get_settings
from saral.db.models import AuditLog, Message
from saral.db.session import get_sessionmaker
from saral.logging import get_logger

log = get_logger(__name__)


async def purge_messages(now: datetime | None = None) -> int:
    """Delete redacted messages older than the retention window."""
    settings = get_settings()
    cutoff = (now or datetime.now(UTC)) - timedelta(days=settings.message_retention_days)
    sm = get_sessionmaker()
    async with sm() as session:
        res = await session.execute(delete(Message).where(Message.created_at < cutoff))
        await session.commit()
        return int(getattr(res, "rowcount", 0) or 0)


async def purge_audit(now: datetime | None = None) -> int:
    """Delete audit rows older than the (long) regulatory hold. Normally a no-op."""
    settings = get_settings()
    cutoff = (now or datetime.now(UTC)) - timedelta(days=365 * settings.audit_retention_years)
    sm = get_sessionmaker()
    async with sm() as session:
        res = await session.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        await session.commit()
        return int(getattr(res, "rowcount", 0) or 0)


def purge_dedup() -> int:
    """Purge dedup keys past the pending-write TTL (dedup window tied to TTL, ADR-0004)."""
    from saral.tools.store import get_store

    return get_store().purge_idempotency(get_settings().pending_write_ttl_s)


async def run_purge() -> dict[str, int]:
    counts = {
        "messages": await purge_messages(),
        "audit": await purge_audit(),
        "dedup": purge_dedup(),
    }
    log.info("purge.complete", **counts)
    return counts


def main() -> None:
    asyncio.run(run_purge())


if __name__ == "__main__":
    main()
