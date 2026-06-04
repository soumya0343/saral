"""Supervisor-orchestrated agent graph.

The supervisor routes; specialists work (TRD §11.1). Compliance is a gate, not a peer
(TRD §11.2): it runs sequentially before any action executes, and PII redaction runs before
the response is emitted. Mixed intents fan retrieval and the action branch out in parallel
and converge at synthesis.

    START -> triage -> compliance -> supervisor -(route)->
        { synthesis              (respond / injection-blocked -> escalate)
        | rag -> synthesis       (information)
        | action -> synthesis    (action; gate already enforced via allowed_actions)
        | {rag, action} -> synthesis   (mixed: parallel fan-out) }
    synthesis -> END

Compliance runs on every message (injection + authorization) BEFORE the supervisor routes,
so an injection attempt on any path is caught and the action node only executes allowed
actions.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from saral.agents.action import ActionAgent
from saral.agents.rag import RagAgent
from saral.agents.synthesis import SynthesisAgent, SynthesisContext
from saral.agents.triage import TriageAgent
from saral.compliance.gate import ComplianceGate
from saral.compliance.pii import redact_pii
from saral.config import get_settings
from saral.graph.state import RunState
from saral.schemas import IntentType, Language

_triage = TriageAgent()
_rag = RagAgent()
_action = ActionAgent()
_gate = ComplianceGate()
_synth = SynthesisAgent()

_STATUS_FROM_RESOLUTION = {
    "resolved": "resolved",
    "escalated": "escalated",
    "blocked": "escalated",
    "degraded": "degraded",
}


async def triage_node(state: RunState) -> dict:
    result = await _triage.run(state.raw_message)
    return {
        "language": result.language,
        "intents": result.intents,
        "entities": result.entities,
        "step_count": 1,
    }


def _route(state: RunState) -> str:
    if state.step_count > get_settings().max_step_count:
        return "end"
    kinds = {i.type for i in state.intents}
    has_action = IntentType.ACTION in kinds
    has_info = IntentType.INFORMATION in kinds
    if has_action and has_info:
        return "mixed"
    if has_action:
        return "action"
    if has_info:
        return "rag"
    return "respond"


async def supervisor_node(state: RunState) -> dict:
    return {"route": _route(state), "step_count": 1}


async def rag_node(state: RunState) -> dict:
    passages = await _rag.run(state.raw_message)
    return {"retrieved": passages, "step_count": 1}


async def compliance_node(state: RunState) -> dict:
    decisions = _gate.evaluate(state.raw_message, state.intents, state.user_id, state.entities)
    return {"compliance_decisions": decisions, "step_count": 1}


async def action_node(state: RunState) -> dict:
    allowed = ComplianceGate.allowed_actions(state.compliance_decisions)
    permitted = [i for i in state.intents if i.action in allowed]
    records = await _action.run(
        permitted, state.entities, state.user_id, state.run_id, state.raw_message
    )
    return {"actions": records, "step_count": 1}


async def synthesis_node(state: RunState) -> dict:
    ctx = SynthesisContext(
        language=state.language or Language.EN,
        message=state.raw_message,
        passages=state.retrieved,
        actions=state.actions,
        decisions=state.compliance_decisions,
    )
    response = await _synth.run(ctx)
    # Pre-send gate: redact any PII in the outbound message.
    response.message = redact_pii(response.message)
    status = _STATUS_FROM_RESOLUTION.get(response.resolution_status, "resolved")
    return {"final_response": response, "status": status, "step_count": 1}


def _branch(state: RunState):
    # An injection block short-circuits to synthesis (which escalates).
    if any(
        d.actor == "injection_guard" and d.decision == "block"
        for d in state.compliance_decisions
    ):
        return "synthesis"
    route = state.route
    if route == "rag":
        return "rag"
    if route == "action":
        return "action"
    if route == "mixed":
        return ["rag", "action"]
    return "synthesis"  # respond / end


@lru_cache
def build_graph():
    g = StateGraph(RunState)
    g.add_node("triage", triage_node)
    g.add_node("compliance", compliance_node)
    g.add_node("supervisor", supervisor_node)
    g.add_node("rag", rag_node)
    g.add_node("action", action_node)
    g.add_node("synthesis", synthesis_node)

    g.add_edge(START, "triage")
    g.add_edge("triage", "compliance")  # gate runs on every message
    g.add_edge("compliance", "supervisor")
    g.add_conditional_edges(
        "supervisor",
        _branch,
        {"rag": "rag", "action": "action", "synthesis": "synthesis"},
    )
    g.add_edge("rag", "synthesis")
    g.add_edge("action", "synthesis")
    g.add_edge("synthesis", END)
    return g.compile()
