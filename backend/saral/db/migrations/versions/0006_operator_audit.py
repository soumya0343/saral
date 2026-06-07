"""operator audit on escalations: handled_by + handled_at (ADR-0001 operator is audit-only)

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-07
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("escalations", sa.Column("handled_by", sa.String(64), nullable=True))
    op.add_column(
        "escalations", sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("escalations", "handled_at")
    op.drop_column("escalations", "handled_by")
