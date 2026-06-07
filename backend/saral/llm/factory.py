"""LLM client factory + per-role fallback chains.

`get_llm(role)` returns a FallbackLLM that tries the role's configured providers in order
(per-role routing), skipping unavailable ones and falling back to the next on
LLMError. The stub is always appended last so the system runs with no API keys.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel

from saral.config import get_settings
from saral.llm.anthropic import AnthropicProvider
from saral.llm.base import LLMClient, LLMError, Message
from saral.llm.cerebras import CerebrasProvider
from saral.llm.gemini import GeminiProvider
from saral.llm.groq import GroqProvider
from saral.llm.sarvam import SarvamProvider
from saral.llm.stub import StubProvider
from saral.logging import get_logger

T = TypeVar("T", bound=BaseModel)
log = get_logger(__name__)

_REGISTRY: dict[str, type] = {
    "sarvam": SarvamProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "cerebras": CerebrasProvider,
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

    @property
    def has_real_provider(self) -> bool:
        """True if a non-stub provider is configured + available (a real model is reachable)."""
        return any(p.available and p.name != "stub" for p in self.providers)

    async def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> str:
        return await self._run("complete", messages, max_tokens=max_tokens)

    async def structured(
        self, messages: list[Message], schema: type[T], *, max_tokens: int = 1024
    ) -> T:
        return await self._run("structured", messages, schema, max_tokens=max_tokens)

    async def _run(self, method: str, *args, **kwargs):
        last_err: Exception | None = None
        retries = get_settings().llm_retry_cap
        for provider in self.providers:
            if not provider.available:
                continue
            # Retry the same provider a few times (handles malformed/parse errors via
            # re-prompt) before falling through to the next provider.
            for attempt in range(retries + 1):
                try:
                    return await getattr(provider, method)(*args, **kwargs)
                except LLMError as e:
                    last_err = e
                    log.warning(
                        "llm.retry" if attempt < retries else "llm.fallback",
                        provider=provider.name,
                        method=method,
                        attempt=attempt,
                        error=str(e),
                    )
                    continue
        raise LLMError(f"all providers failed for {method}: {last_err}")


@lru_cache
def get_llm(role: str = "default") -> FallbackLLM:
    """Build (and cache) the FallbackLLM for a role's provider chain."""
    settings = get_settings()
    chain = settings.role_chain(role) if role != "default" else settings.provider_chain
    providers: list[LLMClient] = []
    for key in chain:
        cls = _REGISTRY.get(key)
        if cls is None:
            log.warning("llm.unknown_provider", provider=key)
            continue
        providers.append(cls())
    if not any(p.name == "stub" for p in providers):
        providers.append(StubProvider())
    log.info("llm.configured", role=role, chain=[p.name for p in providers])
    return FallbackLLM(providers)
