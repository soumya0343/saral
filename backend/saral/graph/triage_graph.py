"""Phase 1 graph: a single triage node.

Built as a real LangGraph StateGraph so Phase 2 can attach the supervisor + specialist
nodes and conditional edges. The worker drives it with `astream` to surface per-node
progress as trace events.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from saral.agents.triage import TriageAgent
from saral.graph.state import RunState

_triage = TriageAgent()


async def triage_node(state: RunState) -> dict:
    result = await _triage.run(state.raw_message)
    return {
        "language": result.language,
        "intents": result.intents,
        "entities": result.entities,
        "step_count": state.step_count + 1,
        "status": "resolved",  # Phase 1 terminates after triage
    }


@lru_cache
def build_graph():
    graph = StateGraph(RunState)
    graph.add_node("triage", triage_node)
    graph.add_edge(START, "triage")
    graph.add_edge("triage", END)
    return graph.compile()
