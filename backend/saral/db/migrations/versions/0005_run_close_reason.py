"""run lifecycle: close_reason + closed_at on agent_runs (CONTEXT 'Run')

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-07
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Abnormal closes are never context-loss: record terminal reason + timestamp.
    op.add_column("agent_runs", sa.Column("close_reason", sa.String(16), nullable=True))
    op.add_column(
        "agent_runs", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "closed_at")
    op.drop_column("agent_runs", "close_reason")
