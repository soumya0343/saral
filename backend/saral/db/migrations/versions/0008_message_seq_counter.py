"""per-conversation message sequence counter

`max(sequence_num)+1` races when the API (user turn) and the worker (assistant turn) insert
concurrently: both read the same max, one insert violates the unique index, and the worker's
whole persist (run, actions, audit, escalation, suspend custody) was lost. Sequence numbers are
now allocated by an atomic `UPDATE conversations SET next_seq = next_seq + 1 ... RETURNING`,
which row-locks the conversation for the duration of the transaction.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("next_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE conversations c SET next_seq = COALESCE("
        "(SELECT MAX(m.sequence_num) FROM messages m WHERE m.conversation_id = c.id), 0)"
    )


def downgrade() -> None:
    op.drop_column("conversations", "next_seq")
