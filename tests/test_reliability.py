"""Phase 5: provider failover, structured-retry cap, partial-failure isolation, and
checkpoint resume after a simulated worker crash."""

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from saral.agents import rag as rag_module
from saral.graph.build import build_graph
from saral.graph.state import RunState
from saral.llm.base import LLMError, Message
from saral.llm.factory import FallbackLLM
from saral.llm.stub import StubProvider
from saral.tools import mock_backend as mb


def setup_function():
    mb._reset_state()


class _Out(BaseModel):
    ok: bool = True


# --- Provider failover ---


class _DeadProvider:
    name = "dead"
    available = True

    async def complete(self, messages, *, max_tokens=1024):
        raise LLMError("provider down")

    async def structured(self, messages, schema, *, max_tokens=1024):
        raise LLMError("provider down")


async def test_failover_to_next_provider():
    llm = FallbackLLM([_DeadProvider(), StubProvider()])
    # Stub has a complete handler fallback -> returns text, proving failover past the dead one.
    out = await llm.complete([Message(role="user", content="hi")])
    assert isinstance(out, str)


async def test_all_providers_failing_raises():
    llm = FallbackLLM([_DeadProvider()])
    with pytest.raises(LLMError):
        await llm.complete([Message(role="user", content="hi")])


# --- Structured retry-with-cap ---


class _FlakyProvider:
    name = "flaky"
    available = True

    def __init__(self, fail_times: int):
        self.calls = 0
        self.fail_times = fail_times

    async def complete(self, messages, *, max_tokens=1024):
        return "ok"

    async def structured(self, messages, schema, *, max_tokens=1024):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise LLMError("malformed output")
        return schema()


async def test_structured_retry_recovers(monkeypatch):
    # Provider fails twice then succeeds; default retry cap (2) should recover without fallback.
    monkeypatch.setenv("LLM_RETRY_CAP", "2")
    flaky = _FlakyProvider(fail_times=2)
    llm = FallbackLLM([flaky])
    # get_settings is cached; patch the cap directly on the call path via config.
    from saral.config import get_settings

    get_settings.cache_clear()
    out = await llm.structured([Message(role="user", content="x")], _Out)
    assert out.ok
    assert flaky.calls == 3


# --- Partial-failure isolation ---


async def test_rag_failure_degrades_not_crashes(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("retriever exploded")

    monkeypatch.setattr(rag_module.RagAgent, "run", boom)
    build_graph.cache_clear()  # rebuild so the patched agent is used
    graph = build_graph()
    state = RunState(run_id="r", conversation_id="c", user_id="U1001",
                     raw_message="What does my policy cover?")
    result = RunState.model_validate(await graph.ainvoke(state))
    build_graph.cache_clear()

    assert "rag" in result.degraded_agents
    assert result.status == "degraded"
    assert result.final_response is not None  # still produced a response


# --- Checkpoint resume after crash ---


class _CkState(BaseModel):
    value: int = 0
    done: bool = False


_attempts = {"n": 0}


def _flaky_node(state: _CkState) -> dict:
    _attempts["n"] += 1
    if _attempts["n"] == 1:
        raise RuntimeError("worker crashed mid-run")
    return {"done": True}


async def test_checkpoint_resumes_after_crash():
    saver = MemorySaver()
    g = StateGraph(_CkState)
    g.add_node("step", _flaky_node)
    g.add_edge(START, "step")
    g.add_edge("step", END)
    graph = g.compile(checkpointer=saver)
    config = {"configurable": {"thread_id": "run-1"}}

    _attempts["n"] = 0
    with pytest.raises(RuntimeError):
        await graph.ainvoke(_CkState(value=5), config)

    # Resume: same thread_id, no new input -> picks up from the checkpoint and completes.
    result = await graph.ainvoke(None, config)
    assert result["done"] is True
    assert result["value"] == 5  # earlier state preserved
    assert _attempts["n"] == 2
