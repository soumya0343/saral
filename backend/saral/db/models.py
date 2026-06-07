"""ORM models. All tables carry `tenant_id` (multi-institution ready, even single-tenant).

Phase 1: conversations + messages. Later phases: agent_runs, action_records, audit_log,
eval_results. v2 adds suspend/resume state on the conversation, an escalations table, and
tokenized (no-raw-PII) audit refs.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saral.db.base import Base, TimestampMixin, uuid_str

_DEFAULT_TENANT = "t_demo"


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(64), default=_DEFAULT_TENANT, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    consent_status: Mapped[str] = mapped_column(String(16), default="granted", nullable=False)

    # --- suspend/resume custody ---
    # The current session token (mock IdP custody for the demo); re-validated on every resume.
    session_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    suspend_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pending_write: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    challenge_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The message that triggered the suspend, re-sent on resume so the write re-evaluates.
    original_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.sequence_num",
    )


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(64), default=_DEFAULT_TENANT, nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False)  # PII-redacted before store
    sequence_num: Mapped[int] = mapped_column(Integer, nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_messages_conversation_seq", "conversation_id", "sequence_num", unique=True),
    )


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(64), default=_DEFAULT_TENANT, nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    route: Mapped[str | None] = mapped_column(String(16), nullable=True)
    step_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Terminal close: a run that ends — normally or abnormally — records why
    # and when, so an abnormal close is never silent context-loss. Null while suspended.
    close_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ActionRecordRow(Base, TimestampMixin):
    __tablename__ = "action_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(64), default=_DEFAULT_TENANT, nullable=False)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    args: Mapped[dict] = mapped_column(JSONB, default=dict)  # redacted
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    intent_nonce: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # intent_logged | result | abandoned
    state: Mapped[str] = mapped_column(String(16), default="result", nullable=False)
    ok: Mapped[bool] = mapped_column(default=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class AuditLog(Base, TimestampMixin):
    """Append-only, hash-chained decision log for tamper evidence.

    Holds NO raw PII — only reason codes and tokenized refs (`user_ref` = hash(tenant,user),
    `args_hash` = hash(canonical_args)) — so the 7-year hold stays erasure-compatible.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(64), default=_DEFAULT_TENANT, nullable=False)
    run_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # allow|block|escalate
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    user_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)  # tokenized
    args_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # tokenized
    hash_prev: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_self: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index("ix_audit_run_seq", "run_id", "seq", unique=True),)


class Escalation(Base, TimestampMixin):
    """Human-handoff record. Terminal for the run; the conversation stays durable."""

    __tablename__ = "escalations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
    tenant_id: Mapped[str] = mapped_column(String(64), default=_DEFAULT_TENANT, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    detected_intent: Mapped[str] = mapped_column(String(64), nullable=False)
    blocking_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    attempted_actions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pending_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transcript_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    sla_target: Mapped[str] = mapped_column(String(16), nullable=False)
    operator_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Operator audit: the operator is audit-only — never a graph actor. We record
    # who handled the escalation and when, but an operator never drives a turn or fires a tool.
    handled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvalResult(Base, TimestampMixin):
    __tablename__ = "eval_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
    scenario_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    config_version: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    passed: Mapped[bool] = mapped_column(default=False)
