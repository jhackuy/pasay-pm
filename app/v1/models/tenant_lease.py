"""Tenant + Lease ORM.

AGENTS.md §4 invariants:
- Decimal money (NUMERIC(14,2)) — never float.
- UTC-aware timestamps.
- Lease state machine: DRAFT → ACTIVE → TERMINATED | EXPIRED (CHECK constraint).
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
)
from sqlalchemy.orm import relationship

from app.v1.models.base import TimestampMixin, V1Base, big_pk


LEASE_STATES = ("DRAFT", "ACTIVE", "TERMINATED", "EXPIRED")


class Tenant(V1Base, TimestampMixin):
    __tablename__ = "v1_tenants"
    __table_args__ = (
        Index("ix_v1_tenants_org_id", "org_id"),
    )

    id = big_pk()
    org_id = Column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id = Column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    full_name = Column(String(120), nullable=False)
    contact_phone = Column(String(32), nullable=True)
    contact_email = Column(String(120), nullable=True)


class Lease(V1Base, TimestampMixin):
    __tablename__ = "v1_leases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('DRAFT','ACTIVE','TERMINATED','EXPIRED')",
            name="ck_v1_leases_state",
        ),
        Index("ix_v1_leases_org_id", "org_id"),
        Index("ix_v1_leases_unit_id", "unit_id"),
        Index("ix_v1_leases_tenant_id", "tenant_id"),
    )

    id = big_pk()
    org_id = Column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    unit_id = Column(
        BigInteger,
        ForeignKey("v1_units.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tenant_id = Column(
        BigInteger,
        ForeignKey("v1_tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    monthly_rent = Column(Numeric(14, 2), nullable=False)
    deposit = Column(Numeric(14, 2), nullable=False, default=0)
    state = Column(String(16), nullable=False, default="DRAFT")