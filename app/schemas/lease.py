from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models import LeaseStatus
from app.schemas.common import AuditFields, money_field


class LeaseBase(BaseModel):
    unit_id: int
    tenant_id: int
    start_date: date
    end_date: date
    accounting_start_date: date | None = None
    monthly_rent: Decimal = money_field(gt=0)
    deposit: Decimal = money_field(ge=0, default=Decimal("0.00"))
    status: LeaseStatus = LeaseStatus.active
    due_day: int | None = Field(default=None, ge=1, le=31)
    notes: str | None = None
    renewal_notice_period_days: int | None = Field(default=None, ge=0, le=365)
    management_fee_included: bool | None = None
    special_terms: str | None = None

    @model_validator(mode="after")
    def check_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.accounting_start_date is not None:
            if self.accounting_start_date < self.start_date:
                raise ValueError("accounting_start_date must not be before start_date")
            if self.accounting_start_date > self.end_date:
                raise ValueError("accounting_start_date must not be after end_date")
        return self


class LeaseCreate(LeaseBase):
    pass


class LeaseUpdate(BaseModel):
    unit_id: int | None = None
    tenant_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    accounting_start_date: date | None = None
    monthly_rent: Decimal | None = money_field(gt=0, default=None)
    deposit: Decimal | None = money_field(ge=0, default=None)
    status: LeaseStatus | None = None
    due_day: int | None = Field(default=None, ge=1, le=31)
    notes: str | None = None
    renewal_notice_period_days: int | None = Field(default=None, ge=0, le=365)
    management_fee_included: bool | None = None
    special_terms: str | None = None

    @model_validator(mode="after")
    def check_accounting_start_date(self):
        if self.accounting_start_date is not None:
            if self.start_date is not None and self.accounting_start_date < self.start_date:
                raise ValueError("accounting_start_date must not be before start_date")
            if self.end_date is not None and self.accounting_start_date > self.end_date:
                raise ValueError("accounting_start_date must not be after end_date")
        return self


class LeaseRead(LeaseBase, AuditFields):
    id: int
    move_out_inspection_id: int | None = None
    deposit_settlement_id: int | None = None
    moved_out_settled_at: datetime | None = None
    renewal_metadata: dict | None = None


class LeaseRenewalRequest(BaseModel):
    unit_id: int | None = None
    tenant_id: int | None = None
    start_date: date
    end_date: date
    monthly_rent: Decimal = money_field(gt=0)
    deposit: Decimal = money_field(ge=0, default=Decimal("0.00"))
    due_day: int | None = Field(default=None, ge=1, le=31)
    renewal_notice_period_days: int | None = Field(default=None, ge=0, le=365)


class LeaseDeclineRenewalRequest(BaseModel):
    reason: str | None = None
    move_out_date: date | None = None


class LeaseAutoExpireResponse(BaseModel):
    id: int
    status: str
    old_status: str
    new_status: str
    already_expired: bool
