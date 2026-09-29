"""Redis run bus.

- API enqueues a RunRequest onto a Redis Stream (XADD); workers consume via a consumer
  group (XREADGROUP), giving at-least-once delivery + XAUTOCLAIM recovery (Phase 5).
- Trace events flow worker -> client through a short per-conversation Redis Stream that the
  SSE endpoint reads (replayable: a late or reconnecting client misses nothing).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

import redis.asyncio as redis

from saral.config import get_settings
from saral.schemas import RunRequest, TraceEvent


@lru_cache
def get_redis() -> redis.Redis:
    # socket_timeout=None so blocking reads (XREADGROUP/pubsub) never raise a read timeout;
    # health checks keep idle connections alive.
    return redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_timeout=None,
        socket_keepalive=True,
        health_check_interval=30,
    )


def trace_stream(conversation_id: str) -> str:
    return f"trace:{conversation_id}"


# Trace events live in a short per-conversation Redis Stream (not fire-and-forget pub/sub), so
# a client that subscribes late or reconnects replays what it missed. Payloads are already
# PII-masked (worker/runner.py); the stream is capped and expires with the conversation idle.
TRACE_MAXLEN = 500
TRACE_TTL_S = 3600

# The payload carries the raw message, so the stream must not grow (or retain) forever: cap it
# approximately, and workers XDEL each entry once it is acked (see worker/main.py).
RUN_STREAM_MAXLEN = 10_000


async def enqueue_run(req: RunRequest) -> str:
    """XADD a run request; returns the stream entry id."""
    r = get_redis()
    settings = get_settings()
    entry_id = await r.xadd(
        settings.run_stream,
        {"payload": req.model_dump_json()},
        maxlen=RUN_STREAM_MAXLEN,
        approximate=True,
    )
    return str(entry_id)


async def ensure_group() -> None:
    """Create the consumer group if absent (idempotent)."""
    r = get_redis()
    settings = get_settings()
    try:
        await r.xgroup_create(
            settings.run_stream, settings.run_consumer_group, id="0", mkstream=True
        )
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def publish_trace(event: TraceEvent) -> None:
    r = get_redis()
    key = trace_stream(event.conversation_id)
    await r.xadd(key, {"e": event.model_dump_json()}, maxlen=TRACE_MAXLEN, approximate=True)
    await r.expire(key, TRACE_TTL_S)


async def subscribe_trace(
    conversation_id: str, run_id: str | None = None, last_event_id: str | None = None
) -> AsyncIterator[tuple[str, str]]:
    """Yield (event_id, raw JSON) for a conversation's trace until the run finishes.

    - `run_id`: replay that run's events from the start of the stream (so subscribing AFTER
      posting the message loses nothing), then follow live; stop at its run_finished/error.
    - `last_event_id`: resume after that event (SSE reconnect).
    - neither: only new events from now on (legacy behaviour).
    """
    r = get_redis()
    key = trace_stream(conversation_id)
    cursor = last_event_id or ("0-0" if run_id else "$")
    while True:
        resp = await r.xread({key: cursor}, count=100, block=15000)
        if not resp:
            continue  # keep waiting; the client disconnect cancels this generator
        # redis-py types this loosely; at runtime: [(key, [(entry_id, {field: value}), ...])].
        batches: list[tuple[str, list[tuple[str, dict[str, str]]]]] = resp  # type: ignore[assignment]
        for _key, entries in batches:
            for entry_id, fields in entries:
                cursor = entry_id
                payload: str = fields["e"]
                event = TraceEvent.model_validate_json(payload)
                if run_id and event.run_id != run_id:
                    continue
                yield entry_id, payload
                if event.type in ("run_finished", "error") and (
                    run_id is None or event.run_id == run_id
                ):
                    return
