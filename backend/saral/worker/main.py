"""Agent worker entrypoint.

Phase 0: a no-op heartbeat loop that proves the worker container boots and reaches Redis.
Phase 1 replaces the body with a Redis Streams consumer group (XREADGROUP) that runs the
LangGraph agent graph.
"""

from __future__ import annotations

import asyncio

import redis.asyncio as redis

from saral.config import get_settings
from saral.logging import get_logger

log = get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    await client.ping()
    log.info("worker.started", stream=settings.run_stream, group=settings.run_consumer_group)
    try:
        while True:
            # Phase 1: replace with XREADGROUP consume + graph execution.
            await asyncio.sleep(5)
            log.debug("worker.heartbeat")
    finally:
        await client.aclose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
