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
    contact_status: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class LeaseContactUpdate(BaseModel):
    """Request body for PATCH /api/v1/leases/{lease_id}/contact."""

    model_config = ConfigDict(extra="forbid")

    contact_status: str = Field(min_length=1, max_length=16)
    note: str | None = Field(default=None, max_length=500)


class LeaseWithTenantCreate(BaseModel):
    """Coverage Matrix Property 2.6: register tenant + create lease in
    one transaction (Mini App ``#/properties/{id}/register-tenant``).
    """

    model_config = ConfigDict(extra="forbid")

    unit_id: int = Field(gt=0)
    tenant_full_name: str = Field(min_length=1, max_length=120)
    tenant_contact_phone: str | None = Field(default=None, max_length=32)
    tenant_contact_email: str | None = Field(default=None, max_length=120)
    start_date: date
    end_date: date
    monthly_rent: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    deposit: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)


class LeaseWithTenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tenant_id: int
    tenant_full_name: str
    lease: LeaseRead


class TenantRead(BaseModel):
    """Tenant read model with archived_at visible."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    full_name: str
    contact_phone: str | None = None
    contact_email: str | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime