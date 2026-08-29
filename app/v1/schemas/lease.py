"""Lease schemas."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class LeaseCreate(BaseModel):
    unit_id: int = Field(gt=0)
    tenant_id: int = Field(gt=0)
    start_date: date
    end_date: date
    monthly_rent: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    deposit: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)


class LeaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    unit_id: int
    tenant_id: int
    start_date: date
    end_date: date
    monthly_rent: Decimal
    deposit: Decimal
    state: str
    created_at: datetime
    updated_at: datetime