"""ORM models. Phase 1 introduces conversations + messages; later phases add
agent_runs, action_records, audit_log, eval_results.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saral.db.base import Base, TimestampMixin, uuid_str


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.sequence_num",
    )


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
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
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    route: Mapped[str | None] = mapped_column(String(16), nullable=True)
    step_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)


class ActionRecordRow(Base, TimestampMixin):
    __tablename__ = "action_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    args: Mapped[dict] = mapped_column(JSONB, default=dict)  # redacted
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ok: Mapped[bool] = mapped_column(default=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class AuditLog(Base, TimestampMixin):
    """Append-only, hash-chained decision log for tamper evidence (TRD §16)."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_str)
    run_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # allow|block|escalate
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    hash_prev: Mapped[str] = mapped_column(String(64), nullable=False)
    hash_self: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index("ix_audit_run_seq", "run_id", "seq", unique=True),)
