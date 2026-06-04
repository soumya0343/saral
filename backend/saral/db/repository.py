"""Run persistence: agent_runs, action_records, and the hash-chained audit_log.

Action args and results are PII-redacted before storage (FR-6, NFR-4). The audit chain is
built with `compliance.audit` so a tamper attempt is detectable via `verify_chain`.
"""

from __future__ import annotations

from saral.compliance import audit
from saral.compliance.pii import redact_pii
from saral.db.models import ActionRecordRow, AgentRun, AuditLog
from saral.db.session import get_sessionmaker
from saral.graph.state import RunState


def _redact_dict(d: dict | None) -> dict | None:
    if not d:
        return d
    return {k: (redact_pii(v) if isinstance(v, str) else v) for k, v in d.items()}


async def persist_run(state: RunState) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(
            AgentRun(
                id=state.run_id,
                conversation_id=state.conversation_id,
                status=state.status,
                route=state.route,
                step_count=state.step_count,
            )
        )
        for rec in state.actions:
            session.add(
                ActionRecordRow(
                    run_id=state.run_id,
                    tool=rec.tool,
                    args=_redact_dict(rec.args),
                    idempotency_key=rec.idempotency_key,
                    ok=rec.ok,
                    result=_redact_dict(rec.result),
                )
            )

        entries = audit.build_chain(
            [(d.decision, d.actor, redact_pii(d.reason)) for d in state.compliance_decisions]
        )
        for e in entries:
            session.add(
                AuditLog(
                    run_id=state.run_id,
                    conversation_id=state.conversation_id,
                    seq=e.seq,
                    decision=e.decision,
                    reason=e.reason,
                    actor=e.actor,
                    hash_prev=e.hash_prev,
                    hash_self=e.hash_self,
                )
            )
        await session.commit()
