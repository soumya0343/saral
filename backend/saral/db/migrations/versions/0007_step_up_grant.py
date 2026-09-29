"""step-up grant on the conversation; stop holding session tokens server-side

The client now holds its own access/refresh tokens, so `conversations.session_token` is no
longer written (column kept for rollback; existing values are wiped — they are credentials).
Step-up becomes a short server-side grant bound to one pending write's idempotency key.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("step_up_for", sa.String(64), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("step_up_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE conversations SET session_token = NULL")


def downgrade() -> None:
    op.drop_column("conversations", "step_up_until")
    op.drop_column("conversations", "step_up_for")
