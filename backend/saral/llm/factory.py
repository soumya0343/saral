"""LLM client factory + fallback chain.

`get_llm()` returns a FallbackLLM that tries providers in the configured order, skipping
unavailable ones, and falling back to the next on LLMError. The stub is always last.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel

from saral.config import get_settings
from saral.llm.anthropic import AnthropicProvider
from saral.llm.base import LLMClient, LLMError, Message
from saral.llm.sarvam import SarvamProvider
from saral.llm.stub import StubProvider
from saral.logging import get_logger

T = TypeVar("T", bound=BaseModel)
log = get_logger(__name__)

_REGISTRY: dict[str, type] = {
    "sarvam": SarvamProvider,
    "anthropic": AnthropicProvider,
    "stub": StubProvider,
}


class FallbackLLM:
    """Chains providers; first available that succeeds wins."""

    def __init__(self, providers: list[LLMClient]) -> None:
        self.providers = providers

    @property
    def name(self) -> str:
        return "+".join(p.name for p in self.providers)

    @property
    def available(self) -> bool:
        return any(p.available for p in self.providers)

    async def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> str:
        return await self._run("complete", messages, max_tokens=max_tokens)

    async def structured(
        self, messages: list[Message], schema: type[T], *, max_tokens: int = 1024
    ) -> T:
        return await self._run("structured", messages, schema, max_tokens=max_tokens)

    async def _run(self, method: str, *args, **kwargs):
        last_err: Exception | None = None
        for provider in self.providers:
            if not provider.available:
                continue
            try:
                return await getattr(provider, method)(*args, **kwargs)
            except LLMError as e:
                last_err = e
                log.warning("llm.fallback", provider=provider.name, method=method, error=str(e))
                continue
        raise LLMError(f"all providers failed for {method}: {last_err}")


@lru_cache
def get_llm() -> FallbackLLM:
    settings = get_settings()
    providers: list[LLMClient] = []
    for key in settings.provider_chain:
        cls = _REGISTRY.get(key)
        if cls is None:
            log.warning("llm.unknown_provider", provider=key)
            continue
        providers.append(cls())
    if not any(p.name == "stub" for p in providers):
        providers.append(StubProvider())
    log.info("llm.configured", chain=[p.name for p in providers])
    return FallbackLLM(providers)
