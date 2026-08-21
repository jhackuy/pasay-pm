from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, SoftDeleteMixin, pg_enum


class TenantContactStatus(str, Enum):
    """PASAY-AI-EMPLOYEE-FOUNDATION-007 §3: tenant contact validation state."""

    UNKNOWN = "UNKNOWN"
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    WRONG_NUMBER = "WRONG_NUMBER"
    UNREACHABLE = "UNREACHABLE"
    CHANGED = "CHANGED"


class Tenant(AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "tenants"

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # PASAY-AI-EMPLOYEE-FOUNDATION-007 §3: structured contact fields. The
    # legacy ``phone`` column doubles as the primary phone for back-compat.
    secondary_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    telegram: Mapped[str | None] = mapped_column(String(100), nullable=True)
    whatsapp: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_status: Mapped[TenantContactStatus | None] = mapped_column(
        pg_enum(TenantContactStatus, "tenant_contact_status"), nullable=True
    )
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_confirmed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # PASAY-AI-EMPLOYEE-FOUNDATION-007 §3.1: tenant identity is SENSITIVE. The
    # structured id number is stored here but the DEFAULT read path redacts it
    # (``id_number`` returns None and ``id_registered`` carries a boolean), so
    # group / Daily Digest / archive bodies can only ever show ``ID：已登记``.
    id_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    id_front_file_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    id_back_file_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # PASAY-AI-EMPLOYEE-FOUNDATION-007 §3.2: structured emergency contact.
    emergency_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    emergency_relationship: Mapped[str | None] = mapped_column(String(100), nullable=True)
    emergency_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @property
    def primary_phone(self) -> str | None:
        return self.phone
