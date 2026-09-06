"""Add the append-only delivery attempt ledger.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from fhirbridge.storage.base import append_only_sql, rls_policy_sql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("tenant_fk", sa.String(length=40), nullable=False),
        sa.Column("conversion_id", sa.String(length=40), nullable=False),
        sa.Column("target_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("validation_status", sa.String(length=20), nullable=False),
        sa.Column("preflight_status", sa.String(length=20), nullable=False),
        sa.Column(
            "human_attested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("reviewer_id", sa.String(length=64), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("resource_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status in ('submitted','failed','denied')",
            name="delivery_attempt_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_fk"],
            ["tenants.id"],
            name="fk_delivery_attempts_tenant_fk_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_delivery_attempts"),
    )
    op.create_index("ix_delivery_attempts_conversion_id", "delivery_attempts", ["conversion_id"])
    op.create_index("ix_delivery_attempts_target_id", "delivery_attempts", ["target_id"])
    op.create_index("ix_delivery_attempts_tenant_id", "delivery_attempts", ["tenant_id"])
    op.create_index(
        "ix_delivery_attempts_tenant_id_created_at",
        "delivery_attempts",
        ["tenant_id", "created_at"],
    )
    for statement in rls_policy_sql("delivery_attempts"):
        op.execute(statement)
    for statement in append_only_sql("delivery_attempts"):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS delivery_attempts_append_only ON delivery_attempts")
    op.execute("DROP FUNCTION IF EXISTS delivery_attempts_forbid_mutation()")
    op.execute("DROP POLICY IF EXISTS delivery_attempts_tenant_isolation ON delivery_attempts")
    op.drop_table("delivery_attempts")
