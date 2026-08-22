from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.deposit_settlement import DepositSettlementStatus
from app.schemas.common import AuditFields, money_field


class DeductionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    amount: Decimal = money_field(ge=0)
    income_id: int | None = None


class DepositSettlementBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deposit_received: Decimal = money_field(ge=0)
    total_deductions: Decimal = money_field(ge=0, default=Decimal("0.00"))
    refund_amount: Decimal = money_field(ge=0, default=Decimal("0.00"))
    deductions: list[DeductionItem] | None = None
    notes: str | None = None


class DepositSettlementCreate(DepositSettlementBase):
    model_config = ConfigDict(extra="forbid")
    move_out_inspection_id: int


class DepositSettlementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deposit_received: Decimal | None = money_field(ge=0, default=None)
    total_deductions: Decimal | None = money_field(ge=0, default=None)
    refund_amount: Decimal | None = money_field(ge=0, default=None)
    deductions: list[DeductionItem] | None = None
    notes: str | None = None


class DepositSettlementConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pass


class DepositSettlementRead(DepositSettlementBase, AuditFields):
    model_config = ConfigDict(extra="allow")
    id: int
    lease_id: int
    move_out_inspection_id: int
    status: DepositSettlementStatus
    confirmed_at: datetime | None = None
    confirmed_by: int | None = None
