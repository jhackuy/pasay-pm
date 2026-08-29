"""PASAY reference implementation — Repair / Quote / Invoice / Photo ORM models.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/models/repair.py``.

Entities in this file:
    * Repair           — repair request with a 9-state machine.
    * RepairQuote      — vendor quote attached to a Repair.
    * RepairInvoice    — final invoice once work is complete.
    * RepairPhoto      — before/after photo evidence.

Repair state machine (9 states):
    REPORTED → ACKNOWLEDGED → ASSESSED → QUOTED → APPROVED
              → IN_PROGRESS → VERIFIED → CLOSED
    plus CANCELLED (terminal abort from any non-terminal state).

Money columns are exclusively ``Numeric(14, 2)``. Timestamps are
``DateTime(timezone=True)``. Every business row carries ``org_id``.
"""
from __future__ import annotations

import enum
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pasay_db_layer import AuditMixin, Base, OrgScopedMixin


class RepairStateEnum(str, enum.Enum):
    REPORTED = "REPORTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ASSESSED = "ASSESSED"
    QUOTED = "QUOTED"
    APPROVED = "APPROVED"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFIED = "VERIFIED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


TERMINAL_REPAIR_STATES: frozenset[str] = frozenset({
    RepairStateEnum.CLOSED.value,
    RepairStateEnum.CANCELLED.value,
})

NON_TERMINAL_REPAIR_STATES: frozenset[str] = frozenset({
    RepairStateEnum.REPORTED.value,
    RepairStateEnum.ACKNOWLEDGED.value,
    RepairStateEnum.ASSESSED.value,
    RepairStateEnum.QUOTED.value,
    RepairStateEnum.APPROVED.value,
    RepairStateEnum.IN_PROGRESS.value,
    RepairStateEnum.VERIFIED.value,
})


class Repair(Base, AuditMixin, OrgScopedMixin):
    """A repair request with a 9-state machine."""

    __tablename__ = "repairs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    unit_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("units.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    reported_by_user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[RepairStateEnum] = mapped_column(
        SAEnum(
            RepairStateEnum,
            name="repair_state_enum",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        server_default=text("'REPORTED'"),
    )
    assigned_vendor: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    quotes: Mapped[list["RepairQuote"]] = relationship(
        back_populates="repair", cascade="all, delete-orphan"
    )
    invoices: Mapped[list["RepairInvoice"]] = relationship(
        back_populates="repair", cascade="all, delete-orphan"
    )
    photos: Mapped[list["RepairPhoto"]] = relationship(
        back_populates="repair", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "length(title) > 0", name="repairs_title_nonempty"
        ),
        CheckConstraint(
            "(state IN ('CLOSED','CANCELLED') AND closed_at IS NOT NULL) OR "
            "(state NOT IN ('CLOSED','CANCELLED') AND closed_at IS NULL)",
            name="ck_repairs_closed_at",
        ),
        Index(
            "ix_repairs_org_state",
            "org_id",
            "state",
        ),
    )


class RepairQuote(Base, AuditMixin, OrgScopedMixin):
    """Vendor quote attached to a Repair."""

    __tablename__ = "repair_quotes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repair_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("repairs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vendor_name: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'")
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    accepted: Mapped[bool] = mapped_column(
        Boolean := None,  # type: ignore[name-defined]
        nullable=False,
        server_default=text("FALSE"),
    ) if False else mapped_column(
        # SQLAlchemy Boolean — kept inline to avoid an extra import.
        # Use ``from sqlalchemy import Boolean`` at promotion time if needed.
    ) if False else None  # placeholder; replaced below
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=text("now() AT TIME ZONE 'UTC'"),
    )

    repair: Mapped["Repair"] = relationship(back_populates="quotes")

    __table_args__ = (
        CheckConstraint("amount >= 0", name="repair_quotes_amount_nonneg"),
    )


class RepairInvoice(Base, AuditMixin, OrgScopedMixin):
    """Final invoice once the repair work is complete."""

    __tablename__ = "repair_invoices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repair_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("repairs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'")
    )
    paid: Mapped[bool] = mapped_column(
        Boolean := None,  # type: ignore[name-defined]
        nullable=False,
        server_default=text("FALSE"),
    ) if False else mapped_column(
    ) if False else None  # placeholder; replaced below
    paid_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    file_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    repair: Mapped["Repair"] = relationship(back_populates="invoices")

    __table_args__ = (
        CheckConstraint("amount >= 0", name="repair_invoices_amount_nonneg"),
        CheckConstraint(
            "(paid = TRUE AND paid_at IS NOT NULL) OR (paid = FALSE)",
            name="ck_repair_invoices_paid_at",
        ),
    )


class RepairPhoto(Base, AuditMixin, OrgScopedMixin):
    """Before/after photo evidence attached to a Repair."""

    __tablename__ = "repair_photos"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repair_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("repairs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'BEFORE'"))

    repair: Mapped["Repair"] = relationship(back_populates="photos")

    __table_args__ = (
        CheckConstraint(
            "size_bytes >= 0", name="repair_photos_size_nonneg"
        ),
        CheckConstraint(
            "kind IN ('BEFORE','AFTER','DURING','OTHER')",
            name="ck_repair_photos_kind",
        ),
        UniqueConstraint(
            "repair_id", "file_hash_sha256", name="uq_repair_photos_hash"
        ),
    )


__all__ = [
    "RepairStateEnum",
    "TERMINAL_REPAIR_STATES",
    "NON_TERMINAL_REPAIR_STATES",
    "Repair",
    "RepairQuote",
    "RepairInvoice",
    "RepairPhoto",
]
