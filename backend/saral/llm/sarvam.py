"""Sarvam provider (OpenAI-compatible chat completions).

Sarvam exposes Indian-language-strong chat models over an OpenAI-style API. Structured
output is requested via JSON mode + schema-in-prompt, then parsed and validated.
"""

from __future__ import annotations

import json
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from saral.config import get_settings
from saral.llm.base import LLMError, Message

T = TypeVar("T", bound=BaseModel)


class SarvamProvider:
    name = "sarvam"

    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def available(self) -> bool:
        return bool(self._settings.sarvam_api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.sarvam_api_key}",
            "Content-Type": "application/json",
        }

    async def _chat(self, payload: dict) -> str:
        url = f"{self._settings.sarvam_base_url.rstrip('/')}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"sarvam request failed: {e}") from e
        msg = data["choices"][0]["message"]
        content = msg.get("content")
        # sarvam-30b/105b are reasoning models: `content` is null until reasoning finishes.
        # An empty content means it ran out of tokens mid-reasoning -> trigger fallback.
        if not content:
            raise LLMError("sarvam returned empty content (reasoning truncated; raise max_tokens)")
        return content

    @staticmethod
    def _to_openai(messages: list[Message]) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in messages]

    async def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> str:
        if not self.available:
            raise LLMError("sarvam: no API key")
        return await self._chat(
            {
                "model": self._settings.sarvam_model,
                "messages": self._to_openai(messages),
                "max_tokens": max_tokens,
            }
        )

    async def structured(
        self, messages: list[Message], schema: type[T], *, max_tokens: int = 1024
    ) -> T:
        if not self.available:
            raise LLMError("sarvam: no API key")
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        instructed = [
            *messages,
            Message(
                role="system",
                content=(
                    "Respond with ONLY a JSON object matching this JSON Schema. "
                    f"No prose, no code fences.\n{schema_json}"
                ),
            ),
        ]
        raw = await self._chat(
            {
                "model": self._settings.sarvam_model,
                "messages": self._to_openai(instructed),
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
        )
        try:
            return schema.model_validate_json(_strip_fences(raw))
        except ValidationError as e:
            raise LLMError(f"sarvam structured parse failed: {e}") from e


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()
