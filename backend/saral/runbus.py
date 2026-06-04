"""Redis run bus.

- API enqueues a RunRequest onto a Redis Stream (XADD); workers consume via a consumer
  group (XREADGROUP), giving at-least-once delivery + XAUTOCLAIM recovery (Phase 5).
- Trace events flow worker -> client over a per-conversation pub/sub channel that the SSE
  endpoint subscribes to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

import redis.asyncio as redis

from saral.config import get_settings
from saral.schemas import RunRequest, TraceEvent


@lru_cache
def get_redis() -> redis.Redis:
    return redis.from_url(get_settings().redis_url, decode_responses=True)


def trace_channel(conversation_id: str) -> str:
    return f"trace:{conversation_id}"


async def enqueue_run(req: RunRequest) -> str:
    """XADD a run request; returns the stream entry id."""
    r = get_redis()
    settings = get_settings()
    return await r.xadd(settings.run_stream, {"payload": req.model_dump_json()})


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
    await r.publish(trace_channel(event.conversation_id), event.model_dump_json())


async def subscribe_trace(conversation_id: str) -> AsyncIterator[str]:
    """Yield raw JSON trace events for a conversation until a run_finished/error arrives."""
    r = get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(trace_channel(conversation_id))
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            payload: str = message["data"]
            yield payload
            event = TraceEvent.model_validate_json(payload)
            if event.type in ("run_finished", "error"):
                break
    finally:
        await pubsub.unsubscribe(trace_channel(conversation_id))
        await pubsub.aclose()
