"""identity v2: tenant_id, suspend/resume state, escalations, tokenized audit refs

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def _tenant_col() -> sa.Column:
    return sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="t_demo")


def upgrade() -> None:
    # tenant_id on every table (multi-institution ready, even single-tenant) — FR-17.
    for table in ("conversations", "messages", "agent_runs", "action_records", "audit_log"):
        op.add_column(table, _tenant_col())

    # Conversation: consent + suspend/resume custody (TRD §13.2/§13.4).
    op.add_column(
        "conversations",
        sa.Column("consent_status", sa.String(16), nullable=False, server_default="granted"),
    )
    op.add_column("conversations", sa.Column("session_token", sa.Text(), nullable=True))
    op.add_column("conversations", sa.Column("suspend_status", sa.String(32), nullable=True))
    op.add_column("conversations", sa.Column("pending_write", JSONB(), nullable=True))
    op.add_column("conversations", sa.Column("challenge_id", sa.String(64), nullable=True))
    op.add_column("conversations", sa.Column("original_message", sa.Text(), nullable=True))

    # action_records: intent nonce + lifecycle state (TRD §16).
    op.add_column(
        "action_records",
        sa.Column("intent_nonce", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "action_records",
        sa.Column("state", sa.String(16), nullable=False, server_default="result"),
    )

    # audit_log: tokenized refs — NO raw PII (NFR-4).
    op.add_column("audit_log", sa.Column("user_ref", sa.String(64), nullable=True))
    op.add_column("audit_log", sa.Column("args_hash", sa.String(64), nullable=True))

    # Escalations — the human-handoff artifact (TRD §12.7).
    op.create_table(
        "escalations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="t_demo"),
        sa.Column("conversation_id", sa.String(32), nullable=False),
        sa.Column("detected_intent", sa.String(64), nullable=False),
        sa.Column("blocking_reason", sa.String(64), nullable=False),
        sa.Column("attempted_actions", JSONB(), nullable=True),
        sa.Column("pending_action", sa.String(64), nullable=True),
        sa.Column("transcript_ref", sa.String(64), nullable=False),
        sa.Column("sla_target", sa.String(16), nullable=False),
        sa.Column("operator_reply", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_escalations_conversation_id", "escalations", ["conversation_id"])


def downgrade() -> None:
    op.drop_table("escalations")
    op.drop_column("audit_log", "args_hash")
    op.drop_column("audit_log", "user_ref")
    op.drop_column("action_records", "state")
    op.drop_column("action_records", "intent_nonce")
    for col in (
        "original_message",
        "challenge_id",
        "pending_write",
        "suspend_status",
        "session_token",
        "consent_status",
    ):
        op.drop_column("conversations", col)
    for table in ("audit_log", "action_records", "agent_runs", "messages", "conversations"):
        op.drop_column(table, "tenant_id")
