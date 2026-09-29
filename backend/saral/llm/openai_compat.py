"""OpenAI-compatible chat provider base.

Most free-tier LLMs we use (Groq, Cerebras, and Gemini via its OpenAI-compat endpoint) speak
the OpenAI `/chat/completions` wire format — the same shape Sarvam exposes. This base
implements `complete()` / `structured()` over it; a concrete provider just supplies
name + base_url + api_key + model. Structured output uses JSON mode + schema-in-prompt, then
parse-and-validate (the lowest-common-denominator that works across all three).
"""

from __future__ import annotations

import asyncio
import json
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from saral.llm import usage
from saral.llm.base import LLMError, Message
from saral.logging import get_logger

T = TypeVar("T", bound=BaseModel)
log = get_logger(__name__)



def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None


def _reasoning_for(model: str) -> tuple[str | None, int]:
    """(reasoning_effort, min max_tokens) for a model id.

    gpt-oss (Groq) cannot disable reasoning: keep it low with a token floor. Gemini 3 Flash
    thinks by default and accepts "none" (support phrasing needs no thinking). Flash-Lite does
    not think and rejects the parameter. Everything else is sent unchanged.
    """
    m = model.lower()
    if "gpt-oss" in m:
        return "low", 512
    if m.startswith("gemini-3") and "flash" in m and "lite" not in m:
        return "none", 0
    return None, 0


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
        # Multiple comma-separated keys are rotated round-robin and on 429, to spread free-tier
        # rate limits across keys before the chain falls through to the next provider.
        self._keys = [k.strip() for k in (api_key or "").split(",") if k.strip()]
        self._rr = 0  # round-robin start pointer
        self._model = model
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        if name:
            self.name = name

    @property
    def available(self) -> bool:
        return bool(self._keys)

    def _http(self) -> httpx.AsyncClient:
        """One pooled client per event loop (connection + TLS reuse across calls)."""
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._client_loop = loop
        return self._client

    async def _chat(self, payload: dict) -> str:
        url = f"{self._base_url}/chat/completions"
        n = len(self._keys)
        last_err: LLMError | None = None
        for i in range(n):
            key = self._keys[(self._rr + i) % n]
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            try:
                resp = await self._http().post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                # Groq/OpenAI reject malformed JSON-mode output with a 400; that is the MODEL's
                # output failing, which a retry can fix — unlike a bad key or retired model.
                bad_output = status == 400 and "json_validate_failed" in e.response.text
                last_err = LLMError(
                    f"{self.name} request failed: HTTP {status}"
                    + (" (model produced invalid JSON)" if bad_output else ""),
                    # 429 (rate limit) and 5xx (overload) can recover; other 4xx (bad key,
                    # retired model, bad request) will fail the same way again.
                    retryable=status == 429 or status >= 500 or bad_output,
                    retry_after=_retry_after(e.response),
                    prefer_failover=True,
                )
                if status == 429 and n > 1:
                    log.info("llm.key_rotate", provider=self.name, reason="429", key_index=i)
                    continue  # this key is rate-limited; try the next one
                raise last_err from e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise LLMError(f"{self.name} transport error: {e!r}", prefer_failover=True) from e
            except Exception as e:  # noqa: BLE001 — normalize to LLMError for fallback
                raise LLMError(f"{self.name} request failed: {e}") from e
            # Success — advance the pointer so the NEXT call starts on a different key (spread).
            self._rr = (self._rr + i + 1) % n
            usage.add(int((data.get("usage") or {}).get("total_tokens") or 0))
            try:
                content = data["choices"][0]["message"].get("content")
            except (KeyError, IndexError, TypeError) as e:
                raise LLMError(f"{self.name} malformed response: {e}") from e
            if not content:
                raise LLMError(f"{self.name} returned empty content")
            return content
        assert last_err is not None
        raise LLMError(
            f"{self.name}: all {n} keys rate-limited (429)",
            retryable=True,
            retry_after=last_err.retry_after,
            prefer_failover=True,
        )

    def _payload(self, messages: list[Message], max_tokens: int) -> dict:
        payload: dict = {
            "model": self._model,
            "messages": self._to_openai(messages),
            "max_tokens": max_tokens,
        }
        # Reasoning models spend hidden thinking tokens out of max_tokens, so a short call can come
        # back with empty content. Pin the effort per model family (verified live 2026-09-29).
        effort, floor = _reasoning_for(self._model)
        if effort is not None:
            payload["reasoning_effort"] = effort
        payload["max_tokens"] = max(max_tokens, floor)
        return payload

    @staticmethod
    def _to_openai(messages: list[Message]) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in messages]

    async def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> str:
        if not self.available:
            raise LLMError(f"{self.name}: no API key")
        return await self._chat(self._payload(messages, max_tokens))

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
        payload = self._payload(instructed, max_tokens)
        payload["response_format"] = {"type": "json_object"}
        raw = await self._chat(payload)
        try:
            return schema.model_validate_json(strip_fences(raw))
        except ValidationError as e:
            raise LLMError(f"{self.name} structured parse failed: {e}") from e
