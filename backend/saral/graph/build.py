"""Supervisor-orchestrated agent graph.

The supervisor routes; specialists work (TRD §11.1). Conditional edges keyed on the
triage agent's structured intent send information-only requests to RAG and action requests
to the Account-Action agent. A hard step-count cap guards against non-termination.

Phase 2 topology:  START -> triage -> supervisor -(route)-> {rag | action} -> END
Phase 3 adds the compliance gate, synthesis, and parallel fan-out for mixed intents.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from saral.agents.action import ActionAgent
from saral.agents.rag import RagAgent
from saral.agents.triage import TriageAgent
from saral.config import get_settings
from saral.graph.state import RunState
from saral.schemas import IntentType

_triage = TriageAgent()
_rag = RagAgent()
_action = ActionAgent()


async def triage_node(state: RunState) -> dict:
    result = await _triage.run(state.raw_message)
    return {
        "language": result.language,
        "intents": result.intents,
        "entities": result.entities,
        "step_count": state.step_count + 1,
    }


def _route(state: RunState) -> str:
    """Data-dependent routing decision based on classified intents."""
    if state.step_count > get_settings().max_step_count:
        return "end"
    kinds = {i.type for i in state.intents}
    if IntentType.ACTION in kinds:
        return "action"
    if IntentType.INFORMATION in kinds:
        return "rag"
    return "end"


async def supervisor_node(state: RunState) -> dict:
    return {"route": _route(state), "step_count": state.step_count + 1}


async def rag_node(state: RunState) -> dict:
    passages = await _rag.run(state.raw_message)
    return {"retrieved": passages, "status": "resolved", "step_count": state.step_count + 1}


async def action_node(state: RunState) -> dict:
    records = await _action.run(
        state.intents, state.entities, state.user_id, state.run_id, state.raw_message
    )
    status = "escalated" if any(r.needs_clarification for r in records) else "resolved"
    return {"actions": records, "status": status, "step_count": state.step_count + 1}


def _branch(state: RunState) -> str:
    return state.route or "end"


@lru_cache
def build_graph():
    g = StateGraph(RunState)
    g.add_node("triage", triage_node)
    g.add_node("supervisor", supervisor_node)
    g.add_node("rag", rag_node)
    g.add_node("action", action_node)

    g.add_edge(START, "triage")
    g.add_edge("triage", "supervisor")
    g.add_conditional_edges(
        "supervisor",
        _branch,
        {"rag": "rag", "action": "action", "end": END},
    )
    g.add_edge("rag", END)
    g.add_edge("action", END)
    return g.compile()
