"""Tenant schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class TenantCreate(BaseModel):
    user_id: int | None = Field(default=None, gt=0)
    full_name: str = Field(min_length=1, max_length=120)
    contact_phone: str | None = Field(default=None, max_length=32)
    contact_email: EmailStr | None = None


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    user_id: int | None
    full_name: str
    contact_phone: str | None
    contact_email: str | None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime