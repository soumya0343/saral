"""LLM client factory + per-role fallback chains.

`get_llm(role)` returns a FallbackLLM that tries the role's configured providers in order
(per-role routing), skipping unavailable ones and falling back to the next on
LLMError. The stub is always appended last so the system runs with no API keys.
"""

from __future__ import annotations

import asyncio
import random
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
# OpenAI-compatible providers that accept a per-role model override.
_MODEL_OVERRIDABLE = {"gemini", "groq", "cerebras"}


_MIN_ATTEMPT_S = 2.5  # never give a real provider less than this (unless less is left)


class _Breaker:
    """Consecutive-failure circuit breaker for one provider in one chain."""

    __slots__ = ("failures", "open_until")

    def __init__(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def is_open(self, now: float) -> bool:
        return now < self.open_until

    def success(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def failure(self, now: float, *, hard: bool) -> None:
        s = get_settings()
        self.failures += 1
        # A non-retryable error (bad key / retired model) opens it at once: it won't heal soon.
        if hard or self.failures >= s.llm_breaker_threshold:
            self.open_until = now + s.llm_breaker_cooldown_s


class FallbackLLM:
    """Chains providers; the first available one that succeeds wins.

    - A per-role deadline bounds the whole call (retries + fallbacks); when it runs out, only
      the deterministic stub (instant) is still tried, so callers always get an answer fast.
    - Same-provider retries only for retryable errors (429, 5xx, timeout, malformed output),
      with jittered exponential backoff, honouring Retry-After when it fits in the deadline.
    - A circuit breaker skips a provider that keeps failing, so a dead model costs ~0 ms per
      turn instead of a full timeout.
    """

    def __init__(self, providers: list[LLMClient], deadline_s: float | None = None) -> None:
        self.providers = providers
        self.deadline_s = deadline_s
        self._breakers: dict[int, _Breaker] = {id(p): _Breaker() for p in providers}

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
        s = get_settings()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.deadline_s if self.deadline_s else None
        last_err: Exception | None = None

        def remaining() -> float | None:
            return None if deadline is None else deadline - loop.time()

        live = [p for p in self.providers if p.available]
        for idx, provider in enumerate(live):
            is_stub = provider.name == "stub"
            # Real providers still after this one (breaker-open ones don't count).
            later_real = [
                p
                for p in live[idx + 1 :]
                if p.name != "stub" and not self._breakers[id(p)].is_open(loop.time())
            ]
            breaker = self._breakers.setdefault(id(provider), _Breaker())
            if not is_stub and breaker.is_open(loop.time()):
                log.info("llm.breaker_open", provider=provider.name, method=method)
                continue
            for attempt in range(s.llm_retry_cap + 1):
                left = remaining()
                if not is_stub and left is not None and left <= 0:
                    break  # budget spent: skip straight to the (instant) stub floor
                # Fair share: one slow provider can't eat the budget of the ones after it.
                budget = left
                if left is not None and not is_stub and later_real:
                    budget = max(left / (len(later_real) + 1), min(_MIN_ATTEMPT_S, left))
                try:
                    call = getattr(provider, method)(*args, **kwargs)
                    timed = budget is not None and not is_stub
                    result = await (asyncio.wait_for(call, budget) if timed else call)
                except (LLMError, TimeoutError) as e:
                    err = e if isinstance(e, LLMError) else LLMError(
                        f"{provider.name} exceeded its time budget", prefer_failover=True
                    )
                    last_err = err
                    breaker.failure(loop.time(), hard=not err.retryable)
                    final = (
                        attempt >= s.llm_retry_cap
                        or not err.retryable
                        or (err.prefer_failover and bool(later_real))
                    )
                    log.warning(
                        "llm.fallback" if final else "llm.retry",
                        provider=provider.name,
                        method=method,
                        attempt=attempt,
                        error=str(err),
                    )
                    if final:
                        break
                    await self._backoff(attempt, err.retry_after, remaining())
                    continue
                breaker.success()
                if is_stub and self.has_real_provider:
                    # Real keys are set but every real model failed: the turn is served by
                    # the deterministic floor. Loud, so free-tier drift is never silent.
                    log.error("llm.served_by_stub", method=method, last_error=str(last_err))
                return result
        raise LLMError(f"all providers failed for {method}: {last_err}")

    @staticmethod
    async def _backoff(attempt: int, retry_after: float | None, left: float | None) -> None:
        base = get_settings().llm_backoff_base_s
        delay = retry_after if retry_after is not None else base * (2**attempt)
        delay += random.uniform(0, base)  # noqa: S311 — jitter, not crypto
        if left is not None:
            delay = min(delay, max(left - 0.05, 0.0))
        if delay > 0:
            await asyncio.sleep(delay)


@lru_cache
def get_llm(role: str = "default") -> FallbackLLM:
    """Build (and cache) the FallbackLLM for a role's provider chain."""
    settings = get_settings()
    chain = settings.role_chain(role) if role != "default" else settings.provider_chain
    providers: list[LLMClient] = []
    for spec in chain:
        # "provider" or "provider:model" — free-tier quotas are per model, so a role can pin its
        # own model (e.g. groq:openai/gpt-oss-20b) and roles don't drain each other's quota.
        key, _, model = spec.partition(":")
        cls = _REGISTRY.get(key)
        if cls is None:
            log.warning("llm.unknown_provider", provider=spec)
            continue
        if model and key in _MODEL_OVERRIDABLE:
            providers.append(cls(model=model))
        else:
            if model:
                log.warning("llm.model_override_ignored", provider=key, model=model)
            providers.append(cls())
    if not any(p.name == "stub" for p in providers):
        providers.append(StubProvider())
    deadline = settings.role_deadline(role)
    log.info("llm.configured", role=role, chain=[p.name for p in providers], deadline_s=deadline)
    return FallbackLLM(providers, deadline_s=deadline)
