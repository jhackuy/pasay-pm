from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from app.schemas.common import money_field


class FinancialSummary(BaseModel):
    month: str
    expected_rent_total: Decimal = money_field(ge=0)
    collected_rent: Decimal = money_field(ge=0)
    outstanding_rent: Decimal = money_field()
    total_income: Decimal = money_field(ge=0)
    total_expense: Decimal = money_field(ge=0)
    net_income: Decimal = money_field()
    units_count: int
    occupied_units: int
    vacant_units: int


class OverdueRent(BaseModel):
    lease_id: int
    unit_id: int
    tenant_id: int
    unit: str
    tenant: str
    outstanding: Decimal = money_field()
    days_overdue: int


class MonthlyLeaseSummary(BaseModel):
    lease_id: int
    unit_id: int
    tenant_id: int
    unit: str
    tenant: str
    expected: Decimal = money_field(ge=0)
    collected: Decimal = money_field(ge=0)
    outstanding: Decimal = money_field()


class CommissionSummaryRow(BaseModel):
    agent_id: int
    agent: str
    rule_id: int
    rule: str
    computed_total: Decimal = money_field(ge=0)
    settlements: int


class ReportTask(BaseModel):
    id: int
    title: str
    unit_id: int | None = None
    unit: str | None = None
    status: str
    priority: str
    due_date: date | None = None
    assigned_to: int | None = None
    recurring: bool
    interval_months: int | None = None
    next_due_date: date | None = None


class ExpenseCategoryRow(BaseModel):
    category: str
    amount: Decimal = money_field(ge=0)
    count: int


class ExpenseUnitRow(BaseModel):
    unit_id: int | None
    unit: str | None
    amount: Decimal = money_field(ge=0)
    count: int


class ExpenseSummary(BaseModel):
    month: str | None
    total_amount: Decimal = money_field(ge=0)
    by_category: list[ExpenseCategoryRow]
    by_unit: list[ExpenseUnitRow]
