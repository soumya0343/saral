"""link every escalation to its RM request and its customer

Every escalation now becomes a request for the customer's relationship manager (the
`rm_requests` table beside the core-system store, which holds the encrypted requested change,
the dedup key, the case summary and the status). The escalation row records which request it
became and whose it is, so the RM no longer joins through the conversation.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-30
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("escalations", sa.Column("user_id", sa.String(32), nullable=True))
    op.add_column("escalations", sa.Column("request_id", sa.String(16), nullable=True))
    op.create_index("ix_escalations_user_id", "escalations", ["user_id"])
    op.create_index("ix_escalations_request_id", "escalations", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_escalations_request_id", table_name="escalations")
    op.drop_index("ix_escalations_user_id", table_name="escalations")
    op.drop_column("escalations", "request_id")
    op.drop_column("escalations", "user_id")
