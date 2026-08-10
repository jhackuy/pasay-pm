from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models import ExpenseStatus, IncomeStatus
from app.schemas.common import AuditFields, money_field


class IncomeBase(BaseModel):
    lease_id: int | None = None
    amount: Decimal = money_field(gt=0)
    received_date: date
    payment_method: str | None = Field(default=None, max_length=50)
    status: IncomeStatus
    description: str | None = None


class IncomeCreate(IncomeBase):
    pass


class IncomeUpdate(BaseModel):
    lease_id: int | None = None
    amount: Decimal | None = money_field(gt=0)
    received_date: date | None = None
    payment_method: str | None = Field(default=None, max_length=50)
    description: str | None = None


class IncomeRead(IncomeBase, AuditFields):
    id: int
    confirmed_by: int | None = None
    confirmed_at: datetime | None = None


class ExpenseBase(BaseModel):
    expense_date: date
    due_date: date | None = None
    category: str = Field(min_length=1, max_length=100)
    amount: Decimal = money_field(gt=0)
    payee: str = Field(min_length=1, max_length=200)
    description: str | None = None
    unit_id: int | None = None
    status: ExpenseStatus
    receipt_attachment_id: int | None = None


class ExpenseCreate(ExpenseBase):
    pass


class ExpenseUpdate(BaseModel):
    expense_date: date | None = None
    due_date: date | None = None
    category: str | None = Field(default=None, min_length=1, max_length=100)
    amount: Decimal | None = money_field(gt=0, default=None)
    payee: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    unit_id: int | None = None
    receipt_attachment_id: int | None = None


class ExpenseRead(ExpenseBase, AuditFields):
    id: int
    approved_by: int | None = None
    approved_at: datetime | None = None
