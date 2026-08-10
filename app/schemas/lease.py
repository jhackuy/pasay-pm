from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models import LeaseStatus
from app.schemas.common import AuditFields, money_field


class LeaseBase(BaseModel):
    unit_id: int
    tenant_id: int
    start_date: date
    end_date: date
    monthly_rent: Decimal = money_field(gt=0)
    deposit: Decimal = money_field(ge=0, default=Decimal("0.00"))
    status: LeaseStatus = LeaseStatus.active
    due_day: int | None = Field(default=None, ge=1, le=31)
    notes: str | None = None

    @model_validator(mode="after")
    def check_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class LeaseCreate(LeaseBase):
    pass


class LeaseUpdate(BaseModel):
    unit_id: int | None = None
    tenant_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    monthly_rent: Decimal | None = money_field(gt=0, default=None)
    deposit: Decimal | None = money_field(ge=0, default=None)
    status: LeaseStatus | None = None
    due_day: int | None = Field(default=None, ge=1, le=31)
    notes: str | None = None


class LeaseRead(LeaseBase, AuditFields):
    id: int
