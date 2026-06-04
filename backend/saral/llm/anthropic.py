"""Anthropic provider. Structured output via tool-use forcing the response schema."""

from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel

from saral.config import get_settings
from saral.llm.base import LLMError, Message

T = TypeVar("T", bound=BaseModel)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._settings.anthropic_api_key)

    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        return self._client

    @staticmethod
    def _split(messages: list[Message]) -> tuple[str, list[dict]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        ]
        return system, turns

    async def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> str:
        if not self.available:
            raise LLMError("anthropic: no API key")
        system, turns = self._split(messages)
        try:
            resp = await self._get_client().messages.create(
                model=self._settings.anthropic_model,
                max_tokens=max_tokens,
                system=system or None,
                messages=turns,
            )
        except Exception as e:  # noqa: BLE001 — normalize to LLMError for fallback
            raise LLMError(f"anthropic complete failed: {e}") from e
        return "".join(b.text for b in resp.content if b.type == "text")

    async def structured(
        self, messages: list[Message], schema: type[T], *, max_tokens: int = 1024
    ) -> T:
        if not self.available:
            raise LLMError("anthropic: no API key")
        system, turns = self._split(messages)
        tool = {
            "name": "emit",
            "description": f"Emit a {schema.__name__}.",
            "input_schema": schema.model_json_schema(),
        }
        try:
            resp = await self._get_client().messages.create(
                model=self._settings.anthropic_model,
                max_tokens=max_tokens,
                system=system or None,
                messages=turns,
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit"},
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"anthropic structured failed: {e}") from e

        for block in resp.content:
            if block.type == "tool_use" and block.name == "emit":
                return schema.model_validate(block.input)
        raise LLMError("anthropic: model did not return tool_use")

    # convenience for callers/tests
    @staticmethod
    def _dumps(obj: BaseModel) -> str:
        return json.dumps(obj.model_dump(), ensure_ascii=False)
