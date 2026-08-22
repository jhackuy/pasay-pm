from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.models.deposit_settlement import DepositSettlementStatus
from app.schemas.common import AuditFields, money_field


class DeductionItem(BaseModel):
    description: str
    amount: Decimal = money_field(ge=0)
    income_id: int | None = None


class DepositSettlementBase(BaseModel):
    lease_id: int
    move_out_inspection_id: int
    deposit_received: Decimal = money_field(ge=0)
    total_deductions: Decimal = money_field(ge=0, default=Decimal("0.00"))
    refund_amount: Decimal = money_field(ge=0, default=Decimal("0.00"))
    deductions: list[dict] | None = None
    notes: str | None = None


class DepositSettlementCreate(DepositSettlementBase):
    status: DepositSettlementStatus = DepositSettlementStatus.DRAFT


class DepositSettlementUpdate(BaseModel):
    lease_id: int | None = None
    move_out_inspection_id: int | None = None
    deposit_received: Decimal | None = money_field(ge=0, default=None)
    total_deductions: Decimal | None = money_field(ge=0, default=None)
    refund_amount: Decimal | None = money_field(ge=0, default=None)
    deductions: list[dict] | None = None
    notes: str | None = None
    status: DepositSettlementStatus | None = None


class DepositSettlementConfirm(BaseModel):
    pass


class DepositSettlementRead(DepositSettlementBase, AuditFields):
    id: int
    status: DepositSettlementStatus
    confirmed_at: datetime | None = None
    confirmed_by: int | None = None
