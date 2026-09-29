"""Agent worker: consume run requests from a Redis Stream consumer group and execute them.

- Up to `worker_concurrency` runs execute at once (LLM calls are I/O-bound), but runs of the
  SAME conversation are serialized in this process so turns can't overtake each other.
- The Postgres checkpointer is open for the worker's lifetime, so a run interrupted by a crash
  resumes from its last node when XAUTOCLAIM hands the entry to a live worker.
- While a run executes, a heartbeat keeps its stream entry "fresh" so a slow (but alive) run is
  never reclaimed and executed twice by another worker.
- At startup the embedder and RAG indexes are built before the first entry is read.
- Periodically: orphan reaper, and a one-off model probe at startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
import uuid
from collections import defaultdict

from saral.config import get_settings
from saral.graph.checkpoint import open_checkpointer
from saral.logging import get_logger
from saral.runbus import ensure_group, get_redis
from saral.schemas import RunRequest
from saral.worker.runner import execute_run

log = get_logger(__name__)

_REAP_INTERVAL_S = 300  # how often to sweep for orphaned suspended conversations
_RECLAIM_INTERVAL_S = 15  # how often to look for entries abandoned by dead workers


async def run() -> None:
    async with open_checkpointer():
        await _warm_rag()
        await Worker().loop()


async def _warm_rag() -> None:
    """Load e5 + build the indexes before consuming, so the first run isn't ~30s slower."""
    try:
        from saral.rag.index import warm_up

        await asyncio.to_thread(warm_up)
    except Exception as e:  # noqa: BLE001 — retrieval still loads lazily on first query
        log.warning("worker.rag_warm_failed", error=str(e))


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.r = get_redis()
        self.consumer = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.tasks: set[asyncio.Task] = set()
        self.in_flight: set[str] = set()  # stream entry ids being executed here
        # Per-conversation ordering inside this process.
        self.convo_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def free_slots(self) -> int:
        return max(self.settings.worker_concurrency - len(self.tasks), 0)

    async def loop(self) -> None:
        await ensure_group()
        # Advisory: log which configured free-tier models are actually served (never blocks).
        _probe_task = asyncio.create_task(_probe_models())  # noqa: RUF006 — fire-and-forget
        log.info(
            "worker.started",
            stream=self.settings.run_stream,
            group=self.settings.run_consumer_group,
            consumer=self.consumer,
            concurrency=self.settings.worker_concurrency,
        )
        last_reap = last_reclaim = 0.0
        try:
            while True:
                now = time.time()
                if now - last_reap > _REAP_INTERVAL_S:
                    await _reap()
                    last_reap = now
                if now - last_reclaim > _RECLAIM_INTERVAL_S and self.free_slots:
                    await self._reclaim()
                    last_reclaim = now
                if not self.free_slots:
                    await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
                    continue
                resp = await self.r.xreadgroup(
                    self.settings.run_consumer_group,
                    self.consumer,
                    {self.settings.run_stream: ">"},
                    count=self.free_slots,
                    block=2000,
                )
                if not resp:
                    continue
                # redis-py types this loosely; at runtime: [(stream, [(entry_id, fields), ...])].
                batches: list[tuple[str, list]] = resp  # type: ignore[assignment]
                for _stream, entries in batches:
                    self._spawn(entries)
        finally:
            for t in self.tasks:
                t.cancel()

    def _spawn(self, entries) -> None:
        for entry_id, fields in entries:
            if entry_id in self.in_flight:
                continue  # already running here (reclaimed our own slow entry)
            self.in_flight.add(entry_id)
            task = asyncio.create_task(self._handle(entry_id, fields))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _reclaim(self) -> None:
        try:
            res = await self.r.xautoclaim(
                self.settings.run_stream,
                self.settings.run_consumer_group,
                self.consumer,
                min_idle_time=self.settings.claim_min_idle_ms,
                count=min(self.settings.reclaim_batch, self.free_slots),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("worker.reclaim_failed", error=str(e))
            return
        entries = [e for e in res[1] if e and e[1]]  # skip entries deleted meanwhile
        if entries:
            log.info("worker.reclaimed", count=len(entries))
            self._spawn(entries)

    async def _handle(self, entry_id: str, fields: dict) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(entry_id))
        try:
            req = RunRequest.model_validate_json(fields["payload"])
            log.info("run.received", run_id=req.run_id, entry=entry_id)
            async with self.convo_locks[req.conversation_id]:
                await execute_run(req)
        except Exception as e:  # noqa: BLE001
            log.error("run.failed", entry=entry_id, error=str(e))
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            self.in_flight.discard(entry_id)
            await self.r.xack(self.settings.run_stream, self.settings.run_consumer_group, entry_id)
            # Drop the processed payload (raw customer message) instead of retaining it.
            await self.r.xdel(self.settings.run_stream, entry_id)

    async def _heartbeat(self, entry_id: str) -> None:
        """Reset the entry's idle time while it runs, so XAUTOCLAIM elsewhere leaves it alone."""
        interval = max(self.settings.claim_min_idle_ms / 3000, 1.0)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.r.xclaim(
                    self.settings.run_stream,
                    self.settings.run_consumer_group,
                    self.consumer,
                    min_idle_time=0,
                    message_ids=[entry_id],
                    justid=True,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("worker.heartbeat_failed", entry=entry_id, error=str(e))


async def _probe_models() -> None:
    try:
        from saral.llm.probe import probe_chains

        await probe_chains()
    except Exception as e:  # noqa: BLE001
        log.warning("worker.probe_failed", error=str(e))


async def _reap() -> None:
    """Best-effort orphan sweep; never crash the worker loop."""
    try:
        from saral.jobs.reaper import reap_orphans

        await reap_orphans()
    except Exception as e:  # noqa: BLE001
        log.warning("worker.reap_failed", error=str(e))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
