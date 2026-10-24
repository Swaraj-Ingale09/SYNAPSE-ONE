"""model_usage: durable model-gateway cost ledger (spec 10.4, ADR 0003)

Revision ID: 0002_model_usage
Revises: 0001_initial
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_model_usage"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_usage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # SET NULL, not CASCADE: deleting a job must not erase the money it
        # cost. The ledger is an accounting record, not job-owned payload.
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("alias", sa.String(length=128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("price_known", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="succeeded"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('succeeded','failed')",
            name="ck_model_usage_status",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0",
            name="ck_model_usage_tokens_nonneg",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_usage_created_at", "model_usage", ["created_at"])
    op.create_index("ix_model_usage_provider_model", "model_usage", ["provider", "model_id"])


def downgrade() -> None:
    op.drop_index("ix_model_usage_provider_model", table_name="model_usage")
    op.drop_index("ix_model_usage_created_at", table_name="model_usage")
    op.drop_table("model_usage")
