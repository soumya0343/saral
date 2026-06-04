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
    """Raised by a provider when it cannot serve a request (triggers fallback)."""


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
