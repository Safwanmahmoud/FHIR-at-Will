"""Initial schema: tenancy, auth, policies, audit chain, idempotency.

Also installs the two protections that everything later depends on:
row-level security on every tenant-scoped table, and an append-only trigger on
``audit_events``. Both are database-level on purpose (AGENTS.md 8.2) — a future
handler that forgets to filter by tenant, or that tries to update an audit row,
is refused by Postgres rather than by our good intentions.

Revision ID: 0001
Revises:
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from fhirbridge.storage.base import append_only_sql, rls_policy_sql

_INITIAL_TENANT_SCOPED_TABLES = (
    "tenants",
    "users",
    "api_keys",
    "policies",
    "audit_events",
    "idempotency_keys",
)
_INITIAL_APPEND_ONLY_TABLES = ("audit_events",)

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pg_trgm backs the lexical concept search used for candidate retrieval in
    # the bind stage (M3). Creating it here keeps extension management in one
    # place rather than spread across migrations.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("local_only_mode", sa.Boolean(), nullable=True),
        sa.Column("min_qualification_tier", sa.String(length=20), nullable=True),
        sa.Column("max_cost_usd_per_conversion", sa.String(length=32), nullable=True),
        sa.Column("retention", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # Check constraints are named in their short form because the naming
        # convention in fhirbridge.storage.base expands `%(constraint_name)s`.
        # Passing the expanded name here yields `ck_tenants_ck_tenants_...` and
        # silently drifts from the models.
        sa.CheckConstraint("status in ('active','suspended','deleted')", name="tenant_status"),
        sa.CheckConstraint("tenant_id = id", name="tenant_self_reference"),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )
    op.create_index("ix_tenants_created_at", "tenants", ["created_at"])
    op.create_index("ix_tenants_tenant_id", "tenants", ["tenant_id"])

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("tenant_fk", sa.String(length=40), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status in ('active','disabled')", name="user_status"),
        sa.ForeignKeyConstraint(
            ["tenant_fk"], ["tenants.id"], name="fk_users_tenant_fk_tenants", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
    )
    op.create_index("ix_users_created_at", "users", ["created_at"])
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("tenant_fk", sa.String(length=40), nullable=False),
        sa.Column("prefix", sa.String(length=24), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column(
            "scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["tenant_fk"],
            ["tenants.id"],
            name="fk_api_keys_tenant_fk_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.UniqueConstraint("prefix", name="uq_api_keys_prefix"),
    )
    op.create_index("ix_api_keys_created_at", "api_keys", ["created_at"])
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"])
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_index("ix_api_keys_tenant_id_revoked_at", "api_keys", ["tenant_id", "revoked_at"])

    op.create_table(
        "policies",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("tenant_fk", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="standard"),
        sa.Column(
            "definition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("mode in ('standard','dry_run','strict')", name="policy_mode"),
        sa.ForeignKeyConstraint(
            ["tenant_fk"],
            ["tenants.id"],
            name="fk_policies_tenant_fk_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_policies"),
        sa.UniqueConstraint(
            "tenant_id", "name", "version", name="uq_policies_tenant_id_name_version"
        ),
    )
    op.create_index("ix_policies_created_at", "policies", ["created_at"])
    op.create_index("ix_policies_tenant_id", "policies", ["tenant_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("sequence", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False, server_default="success"),
        sa.Column("actor_type", sa.String(length=20), nullable=False, server_default="system"),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("subject_type", sa.String(length=64), nullable=True),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("outcome in ('success','failure','denied')", name="audit_outcome"),
        sa.CheckConstraint(
            "actor_type in ('user','api_key','system','service')",
            name="audit_actor_type",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.UniqueConstraint("sequence", name="uq_audit_events_sequence"),
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_trace_id", "audit_events", ["trace_id"])
    op.create_index("ix_audit_events_tenant_id_sequence", "audit_events", ["tenant_id", "sequence"])

    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("tenant_fk", sa.String(length=40), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("endpoint", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="in_progress"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resource_id", sa.String(length=40), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status in ('in_progress','completed','failed')",
            name="idempotency_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_fk"],
            ["tenants.id"],
            name="fk_idempotency_keys_tenant_fk_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_keys"),
        sa.UniqueConstraint(
            "tenant_id", "endpoint", "key", name="uq_idempotency_keys_tenant_id_endpoint_key"
        ),
    )
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])
    op.create_index("ix_idempotency_keys_tenant_id", "idempotency_keys", ["tenant_id"])
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])

    for table in _INITIAL_TENANT_SCOPED_TABLES:
        for statement in rls_policy_sql(table):
            op.execute(statement)

    for table in _INITIAL_APPEND_ONLY_TABLES:
        for statement in append_only_sql(table):
            op.execute(statement)


def downgrade() -> None:
    for table in _INITIAL_APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {table}_forbid_mutation()")

    for table in _INITIAL_TENANT_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")

    op.drop_table("idempotency_keys")
    op.drop_table("audit_events")
    op.drop_table("policies")
    op.drop_table("api_keys")
    op.drop_table("users")
    op.drop_table("tenants")
