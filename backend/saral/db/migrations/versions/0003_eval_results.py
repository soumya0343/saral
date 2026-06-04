"""eval_results

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-05
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_results",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("scenario_id", sa.String(length=64), nullable=False),
        sa.Column("config_version", sa.String(length=32), nullable=False),
        sa.Column("metrics", JSONB(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_eval_results_scenario_id", "eval_results", ["scenario_id"])
    op.create_index("ix_eval_results_config_version", "eval_results", ["config_version"])


def downgrade() -> None:
    op.drop_table("eval_results")
