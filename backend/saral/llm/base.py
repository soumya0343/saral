"""LLM provider abstraction.

A provider implements two calls:
  - complete(): free-text completion
  - structured(): JSON-schema/Pydantic-constrained output

Providers are tried in a configured order; the deterministic StubProvider is the final
fallback so the system runs with no API keys.
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class Message(BaseModel):
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMError(RuntimeError):
    """Raised by a provider when it cannot serve a request (triggers fallback).

    `retryable`: worth retrying the SAME provider (rate limit, 5xx, timeout, malformed output).
    False for errors a retry can't fix (bad key, unknown model, bad request) -> go to the next
    provider immediately. `retry_after`: seconds the provider asked us to wait, if it said.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        retry_after: float | None = None,
        prefer_failover: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        # Overload / rate limit / transport: another provider in the chain is a better bet than
        # hammering this one, so retry the same provider only when it is the last real option.
        self.prefer_failover = prefer_failover


@runtime_checkable
class LLMClient(Protocol):
    name: str

    @property
    def available(self) -> bool:
        """Whether this provider is usable (e.g. has an API key)."""
        ...

    async def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> str: ...

    async def structured(
        self, messages: list[Message], schema: type[T], *, max_tokens: int = 1024
    ) -> T: ...
