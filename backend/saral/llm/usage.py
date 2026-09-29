"""Per-run LLM token accounting.

Token usage used to live on shared singletons (`provider.last_total_tokens`,
`FallbackLLM.last_tokens`), which interleave wrongly once runs execute concurrently. It is now a
ContextVar: every asyncio task (each graph node / each worker run) sees its own running total,
and a caller measures what it spent as a delta around its own calls.
"""

from __future__ import annotations

from contextvars import ContextVar

_TOKENS: ContextVar[int] = ContextVar("saral_llm_tokens", default=0)


def add(n: int) -> None:
    if n:
        _TOKENS.set(_TOKENS.get() + n)


def current() -> int:
    return _TOKENS.get()
