"""ORM models (AGENTS.md 8.1).

Scope note: this milestone defines the tables M0 and M1 actually use — tenancy,
authentication, the hash-chained audit log, and idempotency — plus the RLS and
append-only machinery every later table will reuse. The conversion, review,
delivery and evaluation tables land with the milestones that read and write
them, each with its own migration. Shipping 28 empty tables that no code touches
would be untestable and would freeze a schema before its consumers exist. See
docs/adr/0006-milestone-storage.md.

Immutability of ``audit_events`` is enforced by a database trigger, not by
application discipline (AGENTS.md 8.2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fhirbridge.storage.base import Base, CreatedAtMixin, TenantMixin, TimestampMixin

# Columns with a fixed initial value carry both `default` and `server_default`.
# The Python default keeps a freshly constructed object usable before it is
# flushed; the server default means the value is still correct for a row inserted
# by a migration, a psql session or the operator's bootstrap script, none of which
# go through the ORM.


class Tenant(Base, TimestampMixin):
    """An isolation boundary. Every other row belongs to exactly one."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    """Equal to ``id``. Present so RLS policies are uniform across every table."""

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )

    local_only_mode: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Per-tenant override of ``LOCAL_ONLY_MODE``. NULL means inherit the server."""

    min_qualification_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    max_cost_usd_per_conversion: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retention: Mapped[str | None] = mapped_column(String(16), nullable=True)

    users: Mapped[list[User]] = relationship(back_populates="tenant")

    __table_args__ = (
        CheckConstraint("status in ('active','suspended','deleted')", name="tenant_status"),
        CheckConstraint("tenant_id = id", name="tenant_self_reference"),
    )


class User(Base, TenantMixin, TimestampMixin):
    """A human principal. Reviewers sign off as one of these (AGENTS.md 11.3)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_fk: Mapped[str] = mapped_column(
        String(40), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )

    tenant: Mapped[Tenant] = relationship(back_populates="users")

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
        CheckConstraint("status in ('active','disabled')", name="user_status"),
    )


class ApiKey(Base, TenantMixin, TimestampMixin):
    """A sandbox API key (AGENTS.md 14).

    Only the Argon2id hash is stored. ``prefix`` is the short public portion,
    indexed so a lookup does not have to hash against every row, which is what
    makes constant-work verification affordable.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_fk: Mapped[str] = mapped_column(
        String(40), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    prefix: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    """Argon2id PHC string. Never a plaintext or reversible representation."""

    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("prefix", name="uq_api_keys_prefix"),
        Index("ix_api_keys_tenant_id_revoked_at", "tenant_id", "revoked_at"),
    )


class Policy(Base, TenantMixin, TimestampMixin):
    """A named conversion/validation policy (AGENTS.md 11.5)."""

    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_fk: Mapped[str] = mapped_column(
        String(40), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="standard", server_default="standard"
    )
    definition: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "version", name="uq_policies_tenant_id_name_version"),
        CheckConstraint("mode in ('standard','dry_run','strict')", name="policy_mode"),
    )


class AuditEvent(Base, TenantMixin, CreatedAtMixin):
    """A tamper-evident audit record (AGENTS.md 8.2).

    Rows are hash-chained: ``hash = sha256(canonical(payload) || prev_hash)``.
    A missing or altered row breaks the chain and is detectable by replaying it.

    The table is append-only by trigger. ``details`` must never contain PHI or
    secrets (principles 2.6, 2.7); it holds identifiers, outcomes and
    acknowledgements.
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
    """Monotonic per-database ordering, so the chain can be replayed in order."""
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(
        String(20), nullable=False, default="success", server_default="success"
    )
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="system", server_default="system"
    )
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint("outcome in ('success','failure','denied')", name="audit_outcome"),
        CheckConstraint(
            "actor_type in ('user','api_key','system','service')", name="audit_actor_type"
        ),
        Index("ix_audit_events_tenant_id_sequence", "tenant_id", "sequence"),
    )


class IdempotencyKey(Base, TenantMixin, TimestampMixin):
    """Replay protection for state-changing POSTs (AGENTS.md 11).

    ``request_hash`` lets a retry of the *same* request return the stored
    response, while the same key with a *different* body is a
    ``409 idempotency-conflict`` rather than a silent overwrite.
    """

    __tablename__ = "idempotency_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_fk: Mapped[str] = mapped_column(
        String(40), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="in_progress", server_default="in_progress"
    )
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "endpoint", "key", name="uq_idempotency_keys_tenant_id_endpoint_key"
        ),
        CheckConstraint(
            "status in ('in_progress','completed','failed')", name="idempotency_status"
        ),
        # The expiry sweep is the only query that scans this table without a
        # tenant, so it is the one that needs an index of its own.
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )


class DeliveryAttempt(Base, TenantMixin, CreatedAtMixin):
    """Append-only, PHI-free evidence of one target submission attempt."""

    __tablename__ = "delivery_attempts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_fk: Mapped[str] = mapped_column(
        String(40), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    conversion_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    preflight_status: Mapped[str] = mapped_column(String(20), nullable=False)
    human_attested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    reviewer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint(
            "status in ('submitted','failed','denied')",
            name="delivery_attempt_status",
        ),
        Index("ix_delivery_attempts_tenant_id_created_at", "tenant_id", "created_at"),
    )


TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "tenants",
    "users",
    "api_keys",
    "policies",
    "audit_events",
    "idempotency_keys",
    "delivery_attempts",
)
"""Tables that carry ``tenant_id`` and get an RLS policy."""

APPEND_ONLY_TABLES: tuple[str, ...] = ("audit_events", "delivery_attempts")
"""Tables protected by an append-only trigger in this milestone."""


__all__ = [
    "APPEND_ONLY_TABLES",
    "TENANT_SCOPED_TABLES",
    "ApiKey",
    "AuditEvent",
    "DeliveryAttempt",
    "IdempotencyKey",
    "Policy",
    "Tenant",
    "User",
]
