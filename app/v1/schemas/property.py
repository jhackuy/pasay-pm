"""Property + Unit schemas."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PropertyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)


class PropertyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    name: str
    address_line1: str | None
    address_line2: str | None
    city: str | None
    region: str | None
    postal_code: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UnitCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    bedrooms: int = Field(default=0, ge=0, le=50)
    bathrooms: int = Field(default=0, ge=0, le=50)
    monthly_rent: Decimal = Field(ge=0, max_digits=14, decimal_places=2)


class UnitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    property_id: int
    org_id: int
    label: str
    bedrooms: int
    bathrooms: int
    monthly_rent: Decimal
    status: str


class UnitLifecycleEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    unit_id: int
    org_id: int
    kind: str
    from_state: str | None
    to_state: str | None
    note: str | None
    actor_user_id: int | None
    created_at: datetime


class UnitStatusUpdate(BaseModel):
    status: str = Field(pattern="^(AVAILABLE|OCCUPIED|MAINTENANCE|RETIRED)$")
    note: str | None = Field(default=None, max_length=500)


class UnitDetailRead(BaseModel):
    """Unit + its lifecycle history (newest first)."""

    unit: UnitRead
    lifecycle_events: list[UnitLifecycleEventRead]


class UnitEventCreate(BaseModel):
    kind: str = Field(
        pattern="^(STATUS_CHANGE|RENT_CHANGE|ARCHIVED|MAINTENANCE_START|MAINTENANCE_END)$",
    )
    note: str | None = Field(default=None, max_length=500)