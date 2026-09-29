"""Checkpointer lifecycle.

`postgres` persists LangGraph checkpoints so a run interrupted by a worker crash resumes where it
stopped, in any worker process (Redis XAUTOCLAIM hands the entry to a live worker; its
`thread_id` = `run_id` finds the checkpoint). `memory` keeps checkpoints in-process only: enough
for tests and single-process dev.

The async saver is backed by a connection pool opened for the worker's lifetime
(`open_checkpointer`). In prod a configured Postgres backend that can't open is a hard error —
silently degrading to memory would make crash-resume a false claim.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from saral.config import get_settings
from saral.logging import get_logger

log = get_logger(__name__)

_POOL_MAX = 10
_checkpointer: Any = None


def get_checkpointer() -> Any:
    """The active checkpointer (MemorySaver until `open_checkpointer` installs another)."""
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = MemorySaver()
    return _checkpointer


def _set(saver: Any) -> None:
    global _checkpointer
    _checkpointer = saver
    from saral.graph.build import build_checkpointed_graph

    build_checkpointed_graph.cache_clear()  # recompile against the new saver


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[Any]:
    """Install the configured checkpointer for the duration of the context (worker lifespan)."""
    settings = get_settings()
    if settings.checkpoint_backend != "postgres":
        yield get_checkpointer()
        return

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        dsn = settings.database_url.replace("+asyncpg", "").replace("+psycopg", "")
        pool = AsyncConnectionPool(
            dsn,
            max_size=_POOL_MAX,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await pool.open(wait=True, timeout=15)
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()  # idempotent: creates/migrates the checkpoint tables
    except Exception as e:
        if settings.app_env == "prod":
            raise RuntimeError(f"postgres checkpointer unavailable: {e}") from e
        log.warning("checkpoint.postgres_unavailable", error=str(e), fallback="memory")
        yield get_checkpointer()
        return

    log.info("checkpoint.postgres_ready")
    _set(saver)
    try:
        yield saver
    finally:
        _set(MemorySaver())
        await pool.close()
