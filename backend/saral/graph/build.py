"""Supervisor-orchestrated agent graph.

The supervisor routes; specialists work. Identity runs before any data read; Compliance is a
gate, not a peer. State-changing actions pass through step-up → write-confirmation before they
execute, and no write fires without a fresh token re-validation in the same transition.

    START -> triage -> compliance -> identity -> supervisor -(route)->
        { synthesis                 (respond / injection-blocked -> escalate)
        | END                       (suspend: awaiting_input / awaiting_confirmation / abandon)
        | rag -> synthesis          (information; per-customer scoped retrieval)
        | action -> synthesis       (reads, or a CONFIRMED write executed idempotently)
        | {rag, action} -> synthesis (mixed: parallel fan-out) }
    synthesis -> END
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from saral.actions.confirm import build_pending_write, is_stale, parse_confirmation
from saral.agents.action import ActionAgent
from saral.agents.rag import RagAgent
from saral.agents.synthesis import SynthesisAgent, SynthesisContext
from saral.agents.triage import TriageAgent
from saral.auth.identity import identity_gate
from saral.auth.stepup import request_step_up
from saral.authz import allowed_domains as domains_for
from saral.authz import requires_step_up
from saral.compliance.gate import ComplianceGate
from saral.compliance.pii import redact_pii
from saral.config import get_settings
from saral.graph.state import RunState
from saral.logging import get_logger
from saral.schemas import (
    EscalationRecord,
    IntentType,
    Language,
    ResponsePayload,
)

log = get_logger(__name__)

_triage = TriageAgent()
_rag = RagAgent()
_action = ActionAgent()
_gate = ComplianceGate()
_synth = SynthesisAgent()

# Deterministic reply when an info answer is ungrounded (retrieval below floor) — we escalate
# rather than invent a clause we have no passage for.
_UNGROUNDED = {
    Language.EN: "I don't have a confident answer for that, so I'm connecting you with a human agent who can help.",  # noqa: E501
    Language.HI: "मेरे पास इसका भरोसेमंद उत्तर नहीं है, इसलिए मैं आपको एक मानव एजेंट से जोड़ रहा हूँ।",  # noqa: E501
    Language.HINGLISH: "Mere paas iska confident answer nahi hai, isliye main aapko ek human agent se connect kar raha hoon.",  # noqa: E501
}

_STATUS_FROM_RESOLUTION = {
    "resolved": "resolved",
    "escalated": "escalated",
    "blocked": "escalated",
    "degraded": "degraded",
    "awaiting": "in_progress",
}


async def triage_node(state: RunState) -> dict:
    result, degraded = await _triage.run(state.raw_message)
    entities = {**state.known_entities, **result.entities}
    out: dict = {
        "language": result.language,
        "intents": result.intents,
        "entities": entities,
        "step_count": 1,
    }
    # Sarvam language-detect failed -> deterministic fallback served; flag degraded.
    if degraded:
        out["degraded_agents"] = ["triage:sarvam"]
    return out


async def compliance_node(state: RunState) -> dict:
    decisions = _gate.evaluate(state.raw_message, state.intents, state.user_id, state.entities)
    return {"compliance_decisions": decisions, "step_count": 1}


def _injection_blocked(state: RunState) -> bool:
    return any(
        d.actor == "injection_guard" and d.decision == "block"
        for d in state.compliance_decisions
)


def _suspend(status: str, message: str, **extra) -> dict:
    """Terminal suspend update: a user-facing prompt + a non-terminal lifecycle status."""
    up = {
        "route": "suspend",
        "status": status,
        "final_response": ResponsePayload(
            resolution_status="awaiting", message=message, escalated=False
),
        "step_count": 1,
    }
    up.update(extra)
    return up


async def identity_node(state: RunState) -> dict:
    """Identity gate: derive purpose-limitation domains and gate writes.

    Deterministic. Never raises auth_level itself; for a state-changing action it drives
    step-up (OTP) then write-confirmation, suspending the run between turns.
    """
    # Long-term memory is on-demand: the Interaction-history domain is authorized only when
    # the customer's question references past interactions.
    wants_history = bool(state.entities.get("wants_history"))
    up: dict = {
        "allowed_domains": domains_for(state.intents, include_history=wants_history),
        "step_count": 1,
    }

    # Injection already blocks the run — don't mint challenges for a manipulation attempt.
    if _injection_blocked(state):
        return up

    writes = [
        i
        for i in state.intents
        if i.type == IntentType.ACTION and i.action and requires_step_up(i.action)
    ]
    if not writes:
        return up  # reads / info: normal routing, no gate
    intent = writes[0]

    # Stale pending write: past its TTL, execution authority has expired. Discard it so the
    # write is rebuilt fresh (new nonce) and re-earns step-up + confirmation — never auto-fires
    # off an old confirmation ("authority is mortal").
    if state.pending_write is not None and is_stale(state.pending_write):
        log.info("identity.pending_write_stale", run_id=state.run_id, tool=state.pending_write.tool)
        state = state.model_copy(update={"pending_write": None, "challenge_response": None})

    # Resume "no" → abandon the pending write (execution authority is mortal).
    if state.pending_write is not None and parse_confirmation(state.resume_reply or "") == "no":
        return {
            "route": "suspend",
            "status": "resolved",
            "pending_write": None,
            "final_response": ResponsePayload(
                resolution_status="resolved",
                message="Okay, I've cancelled that change. Anything else?",
                escalated=False,
),
            "step_count": 1,
        }

    # Build (first turn) or reuse (resume) the conversation-anchored PendingWrite.
    pending = state.pending_write
    nonce = state.intent_nonce
    if pending is None:
        nonce = state.intent_nonce + 1
        pending = build_pending_write(
            state.conversation_id, intent, state.entities, state.raw_message, nonce
)
        if pending is None:
            # Missing argument → clarification (soft signal), not step-up.
            q = "What should I update — mobile or email — and to what value?"
            return _suspend("awaiting_input", q)

    decision = identity_gate(state.auth_level, state.intents)

    # Step-up owed: auth_level below step_up for a write.
    if decision.owed:
        # A challenge was answered but auth_level is still below step_up → verification failed.
        if state.challenge_response:
            esc = EscalationRecord(
                conversation_id=state.conversation_id,
                tenant_id=state.tenant_id,
                detected_intent=intent.action or "write",
                attempted_actions=[intent.action or ""],
                blocking_reason="step_up_failed",
                transcript_ref=state.conversation_id,
                pending_action=pending.tool,
                sla_target="4h",
)
            return {
                "route": "suspend",
                "status": "escalated",
                "pending_write": pending,
                "escalation": esc,
                "final_response": ResponsePayload(
                    resolution_status="escalated",
                    message=(
                        "I couldn't verify the one-time code, so I've escalated this to a "
                        "human agent who will follow up."
),
                    escalated=True,
),
                "step_count": 1,
            }
        chal = request_step_up(state.user_id)
        prompt = (
            f"To {pending.tool.replace('_', ' ')}, I need to verify it's you. "
            "I've sent a one-time code."
)
        if chal.get("test_otp"):
            prompt += f" (test code: {chal['test_otp']})"
        prompt += " Please reply with the code."
        return _suspend(
            "awaiting_input",
            prompt,
            pending_write=pending,
            intent_nonce=nonce,
            step_up_owed=True,
            challenge_id=chal["challenge_id"],
)

    # auth_level == step_up. Confirmed? → execute; else read back for confirmation.
    if parse_confirmation(state.resume_reply or "") == "yes":
        return {
            "pending_write": pending,
            "intent_nonce": nonce,
            "step_up_owed": False,
            "step_count": 1,
        }  # route stays None → supervisor routes to action/mixed → write executes
    return _suspend(
        "awaiting_confirmation",
        pending.read_back,
        pending_write=pending,
        intent_nonce=nonce,
        step_up_owed=False,
)


def _route(state: RunState) -> str:
    if state.step_count > get_settings().max_step_count:
        return "respond"
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
    # Identity may have already chosen a terminal/suspend route — honour it.
    if state.route is not None:
        return {"step_count": 1}
    return {"route": _route(state), "step_count": 1}


async def rag_node(state: RunState) -> dict:
    # Per-customer scoped retrieval: pass verified user_id + allowed domains.
    try:
        passages = await _rag.run(
            state.raw_message,
            user_id=state.user_id,
            allowed_domains=state.allowed_domains,
)
        # Score-floor groundedness gate: if the best passage is below the
        # floor, the answer would be ungrounded — escalate rather than invent a clause.
        floor = get_settings().retrieval_score_floor
        top = max((p.score for p in passages), default=0.0)
        if floor > 0 and top < floor:
            log.info("rag.below_floor", run_id=state.run_id, top=round(top, 4), floor=floor)
            return {"retrieved": passages, "ungrounded": True, "step_count": 1}
        return {"retrieved": passages, "step_count": 1}
    except Exception as e:  # noqa: BLE001
        log.error("node.failed", node="rag", run_id=state.run_id, error=str(e))
        return {"degraded_agents": ["rag"], "step_count": 1}


async def action_node(state: RunState) -> dict:
    allowed = ComplianceGate.allowed_actions(state.compliance_decisions)
    permitted = [i for i in state.intents if i.action in allowed]
    try:
        records = await _action.run(
            permitted,
            state.entities,
            state.user_id,
            state.run_id,
            state.raw_message,
            pending_write=state.pending_write,
)
        return {"actions": records, "step_count": 1}
    except Exception as e:  # noqa: BLE001
        log.error("node.failed", node="action", run_id=state.run_id, error=str(e))
        return {"degraded_agents": ["action"], "step_count": 1}


async def synthesis_node(state: RunState) -> dict:
    # Ungrounded info answer (retrieval below floor): escalate instead of generating from
    # weak/empty passages. Deterministic wording, no LLM.
    if state.ungrounded:
        lang = state.language or Language.EN
        response = ResponsePayload(
            resolution_status="escalated",
            message=redact_pii(_UNGROUNDED.get(lang, _UNGROUNDED[Language.EN])),
            escalated=True,
)
        primary = next((i.action or str(i.type) for i in state.intents), "unknown")
        return {
            "final_response": response,
            "status": "escalated",
            "step_count": 1,
            "escalation": EscalationRecord(
                conversation_id=state.conversation_id,
                tenant_id=state.tenant_id,
                detected_intent=primary,
                attempted_actions=[],
                blocking_reason="retrieval_below_floor",
                transcript_ref=state.conversation_id,
                sla_target="4h",
),
        }

    ctx = SynthesisContext(
        language=state.language or Language.EN,
        message=state.raw_message,
        passages=state.retrieved,
        actions=state.actions,
        decisions=state.compliance_decisions,
        degraded=state.degraded_agents,
)
    response = await _synth.run(ctx)
    response.message = redact_pii(response.message) # pre-send gate
    status = _STATUS_FROM_RESOLUTION.get(response.resolution_status, "resolved")
    if state.degraded_agents and status == "resolved":
        status = "degraded"
        response.resolution_status = "degraded"

    update: dict = {"final_response": response, "status": status, "step_count": 1}
    # Escalation as a real artifact: emit a record on any escalated outcome.
    if status == "escalated" and state.escalation is None:
        primary = next((i.action or str(i.type) for i in state.intents), "unknown")
        update["escalation"] = EscalationRecord(
            conversation_id=state.conversation_id,
            tenant_id=state.tenant_id,
            detected_intent=primary,
            attempted_actions=[a.tool for a in state.actions],
            blocking_reason="compliance_block_or_low_confidence",
            transcript_ref=state.conversation_id,
            pending_action=state.pending_write.tool if state.pending_write else None,
            sla_target="4h",
)
    return update


def _branch(state: RunState):
    if _injection_blocked(state):
        return "synthesis"  # injection → escalate via synthesis
    route = state.route
    if route == "suspend":
        return "end"
    if route == "rag":
        return "rag"
    if route == "action":
        return "action"
    if route == "mixed":
        return ["rag", "action"]
    return "synthesis"  # respond / end


def _assemble() -> StateGraph:
    g = StateGraph(RunState)
    g.add_node("triage", triage_node)
    g.add_node("compliance", compliance_node)
    g.add_node("identity", identity_node)
    g.add_node("supervisor", supervisor_node)
    g.add_node("rag", rag_node)
    g.add_node("action", action_node)
    g.add_node("synthesis", synthesis_node)

    g.add_edge(START, "triage")
    g.add_edge("triage", "compliance")  # injection + authz gate on every message
    g.add_edge("compliance", "identity")  # identity before any data read; gates writes
    g.add_edge("identity", "supervisor")
    g.add_conditional_edges(
        "supervisor",
        _branch,
        {"rag": "rag", "action": "action", "synthesis": "synthesis", "end": END},
)
    g.add_edge("rag", "synthesis")
    g.add_edge("action", "synthesis")
    g.add_edge("synthesis", END)
    return g


@lru_cache
def build_graph():
    """Compiled graph without a checkpointer — used by eval and tests."""
    return _assemble().compile()


@lru_cache
def build_checkpointed_graph():
    """Compiled graph with a checkpointer so crashed runs resume (worker path)."""
    from saral.graph.checkpoint import get_checkpointer

    return _assemble().compile(checkpointer=get_checkpointer())
