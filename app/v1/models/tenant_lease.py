"""Tenant + Lease ORM.

Rewrite invariants:
- Decimal money (NUMERIC(14,2)) — never float.
- UTC-aware timestamps.
- Lease state machine: DRAFT → ACTIVE → TERMINATED | EXPIRED.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.v1.models.base import BigPK, TimestampMixin, V1Base


LEASE_STATES = ("DRAFT", "ACTIVE", "TERMINATED", "EXPIRED")


class Tenant(V1Base, TimestampMixin):
    __tablename__ = "v1_tenants"
    __table_args__ = (Index("ix_v1_tenants_org_id", "org_id"),)

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("v1_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(120), nullable=True)


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

    id: Mapped[BigPK]
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    unit_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_units.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("v1_tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    start_date: Mapped[object] = mapped_column(Date, nullable=False)
    end_date: Mapped[object] = mapped_column(Date, nullable=False)
    monthly_rent: Mapped[object] = mapped_column(Numeric(14, 2), nullable=False)
    deposit: Mapped[object] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="DRAFT")
