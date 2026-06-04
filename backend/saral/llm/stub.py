"""Deterministic stub provider.

Final fallback when no API key is configured. It cannot infer arbitrary schemas, so
domain modules register deterministic builders keyed on the target schema type via
`register_structured_handler`. This keeps the LLM interface uniform while domain logic
(e.g. keyword-based triage) lives where it belongs and stays reproducible for eval.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from saral.llm.base import LLMError, Message

T = TypeVar("T", bound=BaseModel)

StructuredHandler = Callable[[list[Message]], BaseModel]
CompleteHandler = Callable[[list[Message]], str]

_structured_handlers: dict[type[BaseModel], StructuredHandler] = {}
_complete_handler: CompleteHandler | None = None


def register_structured_handler[S: BaseModel](
    schema: type[S], handler: Callable[[list[Message]], S]
) -> None:
    _structured_handlers[schema] = handler  # type: ignore[assignment]


def register_complete_handler(handler: CompleteHandler) -> None:
    global _complete_handler
    _complete_handler = handler


class StubProvider:
    name = "stub"

    @property
    def available(self) -> bool:
        return True  # always available — it's the floor

    async def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> str:
        if _complete_handler is not None:
            return _complete_handler(messages)
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"), ""
        )
        return f"[stub] {last_user}".strip()

    async def structured(
        self, messages: list[Message], schema: type[T], *, max_tokens: int = 1024
    ) -> T:
        handler = _structured_handlers.get(schema)
        if handler is None:
            raise LLMError(
                f"StubProvider has no handler for {schema.__name__}; "
                "register one via register_structured_handler()"
            )
        return handler(messages)  # type: ignore[return-value]
