"""Run persistence: agent_runs, action_records, the hash-chained audit_log, conversation
suspend/resume state, and escalations.

Action args and results are PII-redacted before storage (FR-6, NFR-4). The audit chain is
built with `compliance.audit`; it stores only reason codes + tokenized refs (`user_ref`,
`args_hash`) — no raw PII (NFR-4). A tamper attempt is detectable via `verify_chain`.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select

from saral.compliance import audit
from saral.compliance.pii import redact_pii
from saral.db.models import (
    ActionRecordRow,
    AgentRun,
    AuditLog,
    Conversation,
    Escalation,
    Message,
)
from saral.db.session import get_sessionmaker
from saral.graph.state import RunState

_SUSPEND_STATUSES = {"awaiting_input", "awaiting_confirmation"}


def _redact_dict(d: dict | None) -> dict | None:
    if not d:
        return d
    return {k: (redact_pii(v) if isinstance(v, str) else v) for k, v in d.items()}


def _tok(*parts: str) -> str:
    """Tokenized ref: a hash standing in for an identifier so the audit log holds no raw PII."""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


async def persist_run(state: RunState) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(
            AgentRun(
                id=state.run_id,
                tenant_id=state.tenant_id,
                conversation_id=state.conversation_id,
                status=state.status,
                route=state.route,
                step_count=state.step_count,
            )
        )
        await session.flush()  # ensure agent_runs row exists before FK-dependent inserts
        for rec in state.actions:
            session.add(
                ActionRecordRow(
                    tenant_id=state.tenant_id,
                    run_id=state.run_id,
                    tool=rec.tool,
                    args=_redact_dict(rec.args),
                    idempotency_key=rec.idempotency_key,
                    intent_nonce=state.intent_nonce,
                    state="result" if rec.ok else "intent_logged",
                    ok=rec.ok,
                    result=_redact_dict(rec.result),
                )
            )

        user_ref = _tok(state.tenant_id, state.user_id)
        args_hash = (
            _tok(json.dumps(state.pending_write.args, sort_keys=True))
            if state.pending_write
            else None
        )
        entries = audit.build_chain(
            [(d.decision, d.actor, redact_pii(d.reason)) for d in state.compliance_decisions]
        )
        for e in entries:
            session.add(
                AuditLog(
                    tenant_id=state.tenant_id,
                    run_id=state.run_id,
                    conversation_id=state.conversation_id,
                    seq=e.seq,
                    decision=e.decision,
                    reason=e.reason,
                    actor=e.actor,
                    user_ref=user_ref,
                    args_hash=args_hash,
                    hash_prev=e.hash_prev,
                    hash_self=e.hash_self,
                )
            )

        # Persist the assistant reply so the conversation transcript is complete end-to-end.
        if state.final_response and state.final_response.message:
            next_seq = (
                await session.scalar(
                    select(func.coalesce(func.max(Message.sequence_num), 0) + 1).where(
                        Message.conversation_id == state.conversation_id
                    )
                )
            ) or 1
            session.add(
                Message(
                    tenant_id=state.tenant_id,
                    conversation_id=state.conversation_id,
                    role="assistant",
                    content=redact_pii(state.final_response.message),
                    sequence_num=next_seq,
                )
            )

        # Suspend/resume custody on the conversation (TRD §13.2/§13.4).
        convo = await session.get(Conversation, state.conversation_id)
        if convo is not None:
            if state.status in _SUSPEND_STATUSES:
                convo.suspend_status = state.status
                convo.pending_write = (
                    state.pending_write.model_dump() if state.pending_write else None
                )
                convo.challenge_id = state.challenge_id
                convo.original_message = state.raw_message
            else:
                # Terminal — clear the suspend custody.
                convo.suspend_status = None
                convo.pending_write = None
                convo.challenge_id = None
                convo.original_message = None
            convo.status = state.status

        # Escalation as a durable artifact (TRD §12.7).
        if state.escalation is not None:
            esc = state.escalation
            session.add(
                Escalation(
                    tenant_id=esc.tenant_id,
                    conversation_id=esc.conversation_id,
                    detected_intent=esc.detected_intent,
                    blocking_reason=esc.blocking_reason,
                    attempted_actions={"tools": esc.attempted_actions},
                    pending_action=esc.pending_action,
                    transcript_ref=esc.transcript_ref,
                    sla_target=esc.sla_target,
                )
            )

        await session.commit()
