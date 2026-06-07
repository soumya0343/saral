"""Sarvam provider (OpenAI-compatible chat completions).

Sarvam exposes Indian-language-strong chat models over an OpenAI-style API. Structured
output is requested via JSON mode + schema-in-prompt, then parsed and validated.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from saral.config import get_settings
from saral.llm.base import LLMError, Message
from saral.schemas import Language

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

    def _lid_headers(self) -> dict[str, str]:
        # Sarvam's classic text APIs (LID/translate) authenticate via api-subscription-key,
        # not the OpenAI-compat Bearer scheme. The same key works for both.
        return {
            "api-subscription-key": self._settings.sarvam_api_key or "",
            "Content-Type": "application/json",
        }

    async def detect_language(self, text: str) -> Language:
        """Language identification via Sarvam's purpose-built /text-lid endpoint.

        Returns one of our three supported labels (en/hi/hinglish). Hinglish is hi written
        in Latin script (script_code), which the deterministic regex can only guess at —
        this is the core Sarvam differentiator (TRD §12.2). Raises LLMError on any transport
        failure so the caller falls back to the deterministic detector and flags degraded
        mode (TRD §15).
        """
        if not self.available:
            raise LLMError("sarvam: no API key")
        url = f"{self._settings.sarvam_base_url.rstrip('/')}/text-lid"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, headers=self._lid_headers(), json={"input": text})
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"sarvam text-lid failed: {e}") from e
        return _map_lid(data, text)

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


_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def _map_lid(data: dict, text: str) -> Language:
    """Map a Sarvam text-lid response to our three-label enum.

    text-lid returns e.g. {"language_code": "hi-IN", "script_code": "Latin"}. Hinglish is the
    (hi, Latin) combination — Hindi words typed in Roman script. Languages outside our enum
    (ta/te/bn/…) fall back to a script heuristic since the corpus only supports en/hi/hinglish.
    """
    code = str(data.get("language_code") or "").lower()
    script = str(data.get("script_code") or "").lower()
    # text-lid returns ISO 15924 codes: "Latn" (Latin/romanized), "Deva" (Devanagari).
    is_latin = script.startswith(("latn", "latin", "roman"))
    if code.startswith("en"):
        return Language.EN
    if code.startswith("hi"):
        return Language.HINGLISH if is_latin else Language.HI
    # Out-of-scope language: best-effort by script (no exception — Sarvam did answer).
    if _DEVANAGARI.search(text):
        return Language.HI
    return Language.EN


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()
