"""R5/R7: retry policy, deadlines, circuit breaker, error classification, per-run token usage."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from saral.llm import usage
from saral.llm.base import LLMError, Message
from saral.llm.factory import FallbackLLM
from saral.llm.stub import StubProvider

MSG = [Message(role="user", content="hi")]


class _Scripted:
    """Provider that replays a script of outcomes: an Exception to raise, or text to return."""

    available = True

    def __init__(self, name: str, script: list, delay: float = 0.0):
        self.name = name
        self.script = list(script)
        self.calls = 0
        self.delay = delay

    async def complete(self, messages, *, max_tokens=1024):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, Exception):
            raise step
        return step

    async def structured(self, messages, schema, *, max_tokens=1024):
        raise NotImplementedError


async def test_non_retryable_error_skips_straight_to_next_provider():
    bad_key = _Scripted("groq", [LLMError("HTTP 401", retryable=False)])
    backup = _Scripted("gemini", ["fine"])
    assert await FallbackLLM([bad_key, backup]).complete(MSG) == "fine"
    assert bad_key.calls == 1  # a 401 won't fix itself: no retries burned


async def test_retryable_error_is_retried_on_same_provider():
    flaky = _Scripted("groq", [LLMError("HTTP 503"), LLMError("HTTP 503"), "recovered"])
    assert await FallbackLLM([flaky]).complete(MSG) == "recovered"
    assert flaky.calls == 3


async def test_breaker_skips_a_dead_provider_on_later_calls():
    dead = _Scripted("groq", [LLMError("HTTP 500")] * 10)
    llm = FallbackLLM([dead, StubProvider()])
    await llm.complete(MSG)  # 3 failures -> breaker opens, stub serves
    calls_after_first = dead.calls
    await llm.complete(MSG)
    assert dead.calls == calls_after_first  # skipped: costs nothing while open


async def test_hard_failure_opens_breaker_immediately():
    retired = _Scripted("gemini", [LLMError("HTTP 404 model gone", retryable=False)] * 5)
    llm = FallbackLLM([retired, StubProvider()])
    await llm.complete(MSG)
    await llm.complete(MSG)
    assert retired.calls == 1


async def test_deadline_bounds_a_slow_provider_and_stub_still_answers():
    slow = _Scripted("gemini", ["too late"], delay=5)
    llm = FallbackLLM([slow, StubProvider()], deadline_s=0.2)
    t0 = asyncio.get_running_loop().time()
    out = await llm.complete(MSG)
    assert out.startswith("[stub]")
    assert asyncio.get_running_loop().time() - t0 < 1.0


async def test_token_usage_is_per_task_not_shared():
    async def spend(n: int) -> int:
        start = usage.current()
        for _ in range(n):
            usage.add(10)
            await asyncio.sleep(0)  # interleave with the other task
        return usage.current() - start

    a, b = await asyncio.gather(spend(3), spend(5))
    assert (a, b) == (30, 50)


@respx.mock
@pytest.mark.parametrize(
    ("status", "retryable"), [(401, False), (404, False), (400, False), (429, True), (503, True)]
)
async def test_http_errors_are_classified(status, retryable):
    from saral.llm.openai_compat import OpenAICompatProvider

    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(status, headers={"retry-after": "2"})
    )
    p = OpenAICompatProvider(base_url="https://llm.test/v1", api_key="k", model="m", name="t")
    with pytest.raises(LLMError) as e:
        await p.complete(MSG)
    assert e.value.retryable is retryable
    if status == 429:
        assert e.value.retry_after == 2.0


@respx.mock
async def test_success_records_usage():
    from saral.llm.openai_compat import OpenAICompatProvider

    respx.post("https://llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hello"}}], "usage": {"total_tokens": 42}},
        )
    )
    p = OpenAICompatProvider(base_url="https://llm.test/v1", api_key="k", model="m", name="t")
    before = usage.current()
    assert await p.complete(MSG) == "hello"
    assert usage.current() - before == 42


async def test_overloaded_provider_fails_over_instead_of_retrying():
    overloaded = _Scripted("gemini", [LLMError("HTTP 503", prefer_failover=True)] * 3)
    backup = _Scripted("groq", ["served"])
    assert await FallbackLLM([overloaded, backup]).complete(MSG) == "served"
    assert overloaded.calls == 1  # an alternative exists: don't burn the budget retrying


async def test_last_real_provider_is_still_retried():
    overloaded = _Scripted("groq", [LLMError("HTTP 503", prefer_failover=True), "ok"])
    assert await FallbackLLM([overloaded, StubProvider()]).complete(MSG) == "ok"
    assert overloaded.calls == 2


async def test_slow_first_provider_leaves_budget_for_the_next():
    slow = _Scripted("gemini", ["late"], delay=5)
    fast = _Scripted("groq", ["on time"])
    llm = FallbackLLM([slow, fast, StubProvider()], deadline_s=6)
    assert await llm.complete(MSG) == "on time"  # slow got ~half the budget, not all of it
