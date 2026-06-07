"""OpenAI-compatible chat provider base.

Most free-tier LLMs we use (Groq, Cerebras, and Gemini via its OpenAI-compat endpoint) speak
the OpenAI `/chat/completions` wire format — the same shape Sarvam exposes. This base
implements `complete()` / `structured()` over it; a concrete provider just supplies
name + base_url + api_key + model. Structured output uses JSON mode + schema-in-prompt, then
parse-and-validate (the lowest-common-denominator that works across all three).
"""

from __future__ import annotations

import json
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from saral.llm.base import LLMError, Message

T = TypeVar("T", bound=BaseModel)


def strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


class OpenAICompatProvider:
    name = "openai-compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        name: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        if name:
            self.name = name

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _chat(self, payload: dict) -> str:
        url = f"{self._base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:  # noqa: BLE001 — normalize to LLMError for fallback
            raise LLMError(f"{self.name} request failed: {e}") from e
        try:
            content = data["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{self.name} malformed response: {e}") from e
        if not content:
            raise LLMError(f"{self.name} returned empty content")
        return content

    @staticmethod
    def _to_openai(messages: list[Message]) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in messages]

    async def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> str:
        if not self.available:
            raise LLMError(f"{self.name}: no API key")
        return await self._chat(
            {
                "model": self._model,
                "messages": self._to_openai(messages),
                "max_tokens": max_tokens,
            }
        )

    async def structured(
        self, messages: list[Message], schema: type[T], *, max_tokens: int = 1024
    ) -> T:
        if not self.available:
            raise LLMError(f"{self.name}: no API key")
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
                "model": self._model,
                "messages": self._to_openai(instructed),
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
        )
        try:
            return schema.model_validate_json(strip_fences(raw))
        except ValidationError as e:
            raise LLMError(f"{self.name} structured parse failed: {e}") from e
