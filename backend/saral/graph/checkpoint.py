"""Checkpointer factory.

`MemorySaver` (default) makes a run resumable within a process — enough to prove
checkpoint-resume in tests. The `postgres` backend persists checkpoints so a crashed worker
resumes a run after restart (needs a live DB + `langgraph-checkpoint-postgres`); it is wired
behind config and falls back to memory if unavailable.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.checkpoint.memory import MemorySaver

from saral.config import get_settings
from saral.logging import get_logger

log = get_logger(__name__)


@lru_cache
def get_checkpointer():
    backend = get_settings().checkpoint_backend
    if backend == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            dsn = get_settings().database_url.replace("+asyncpg", "")
            saver = PostgresSaver.from_conn_string(dsn)
            saver.setup()
            log.info("checkpoint.postgres_ready")
            return saver
        except Exception as e:  # noqa: BLE001
            log.warning("checkpoint.postgres_unavailable", error=str(e))
    return MemorySaver()
