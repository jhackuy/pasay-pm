from enum import Enum
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, SoftDeleteMixin, pg_enum


class CommissionRuleType(str, Enum):
    percentage = "percentage"
    flat = "flat"


class AgentRole(str, Enum):
    rent = "出租"
    sale = "出售"
    management = "管理"


class CommissionRule(AuditMixin, SoftDeleteMixin, Base):
    __tablename__ = "commission_rules"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    rule_type: Mapped[CommissionRuleType] = mapped_column(
        pg_enum(CommissionRuleType, "commission_rule_type"), nullable=False
    )
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    agent_role: Mapped[AgentRole] = mapped_column(
        pg_enum(AgentRole, "agent_role", length=20), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CommissionSettlementStatus(str, Enum):
    pending = "pending"
    confirmed = "confirmed"


class CommissionSettlement(AuditMixin, Base):
    __tablename__ = "commission_settlements"

    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    lease_id: Mapped[int] = mapped_column(ForeignKey("leases.id"), nullable=False, index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("commission_rules.id"), nullable=False, index=True)
    computed_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[CommissionSettlementStatus] = mapped_column(
        pg_enum(CommissionSettlementStatus, "commission_settlement_status"),
        nullable=False,
        default=CommissionSettlementStatus.pending,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
