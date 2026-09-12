"""Index delivery_attempts.created_at, as CreatedAtMixin declares.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_delivery_attempts_created_at", "delivery_attempts", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_delivery_attempts_created_at", table_name="delivery_attempts")
