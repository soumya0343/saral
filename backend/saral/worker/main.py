"""Agent worker: consume run requests from a Redis Stream consumer group and execute them.

Phase 1: runs the triage-only graph and publishes trace events. Phase 5 adds XAUTOCLAIM
recovery of stuck pending messages.
"""

from __future__ import annotations

import asyncio
import socket

from saral.config import get_settings
from saral.logging import get_logger
from saral.runbus import ensure_group, get_redis
from saral.schemas import RunRequest
from saral.worker.runner import execute_run

log = get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    r = get_redis()
    await ensure_group()
    consumer = f"{socket.gethostname()}-{id(object())}"
    log.info(
        "worker.started",
        stream=settings.run_stream,
        group=settings.run_consumer_group,
        consumer=consumer,
    )

    while True:
        # 1) Reclaim messages pending on dead/stuck consumers (XAUTOCLAIM). The checkpointer
        #    lets execute_run resume these mid-run rather than restart (TRD §15).
        await _reclaim(r, settings, consumer)

        # 2) Consume new messages.
        resp = await r.xreadgroup(
            settings.run_consumer_group,
            consumer,
            {settings.run_stream: ">"},
            count=1,
            block=5000,
        )
        if not resp:
            continue
        for _stream, entries in resp:
            await _process_entries(r, settings, entries)


async def _reclaim(r, settings, consumer) -> None:
    try:
        _cursor, entries, _deleted = await r.xautoclaim(
            settings.run_stream,
            settings.run_consumer_group,
            consumer,
            min_idle_time=settings.claim_min_idle_ms,
            count=settings.reclaim_batch,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("worker.reclaim_failed", error=str(e))
        return
    if entries:
        log.info("worker.reclaimed", count=len(entries))
        await _process_entries(r, settings, entries)


async def _process_entries(r, settings, entries) -> None:
    for entry_id, fields in entries:
        try:
            req = RunRequest.model_validate_json(fields["payload"])
            log.info("run.received", run_id=req.run_id, entry=entry_id)
            await execute_run(req)
        except Exception as e:  # noqa: BLE001
            log.error("run.failed", entry=entry_id, error=str(e))
        finally:
            await r.xack(settings.run_stream, settings.run_consumer_group, entry_id)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
