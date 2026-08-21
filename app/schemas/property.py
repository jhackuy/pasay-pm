from decimal import Decimal

from pydantic import BaseModel, Field

from app.models import UnitStatus
from app.schemas.common import AuditFields, money_field


class PropertyBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=500)
    city: str = Field(min_length=1, max_length=100)
    total_units: int = Field(default=0, ge=0)
    is_active: bool = True
    management_company: str | None = Field(default=None, max_length=200)
    management_office_phone: str | None = Field(default=None, max_length=50)
    management_contact_person: str | None = Field(default=None, max_length=200)
    management_email: str | None = Field(default=None, max_length=200)
    management_office_location: str | None = Field(default=None, max_length=300)
    operational_notes: str | None = None


class PropertyCreate(PropertyBase):
    organization_id: int = Field(gt=0)


class PropertyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, min_length=1, max_length=500)
    city: str | None = Field(default=None, min_length=1, max_length=100)
    total_units: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    management_company: str | None = Field(default=None, max_length=200)
    management_office_phone: str | None = Field(default=None, max_length=50)
    management_contact_person: str | None = Field(default=None, max_length=200)
    management_email: str | None = Field(default=None, max_length=200)
    management_office_location: str | None = Field(default=None, max_length=300)
    operational_notes: str | None = None


class PropertyRead(PropertyBase, AuditFields):
    id: int
    organization_id: int | None


class UnitBase(BaseModel):
    property_id: int
    unit_number: str = Field(min_length=1, max_length=50)
    floor: str | None = Field(default=None, max_length=20)
    size_sqm: Decimal | None = Field(default=None, max_digits=10, decimal_places=2)
    monthly_rent: Decimal = money_field(gt=0)
    status: UnitStatus = UnitStatus.vacant
    is_active: bool = True
    unit_state: str | None = Field(default=None, max_length=30)


class UnitCreate(UnitBase):
    pass


class UnitUpdate(BaseModel):
    unit_number: str | None = Field(default=None, min_length=1, max_length=50)
    floor: str | None = Field(default=None, max_length=20)
    size_sqm: Decimal | None = Field(default=None, max_digits=10, decimal_places=2)
    monthly_rent: Decimal | None = money_field(gt=0, default=None)
    status: UnitStatus | None = None
    is_active: bool | None = None
    unit_state: str | None = Field(default=None, max_length=30)


class UnitRead(UnitBase, AuditFields):
    id: int


# --- Issue #25 §4: Unit ↔ Channel minimal binding schemas ------------------

class UnitChannelBindingBase(BaseModel):
    unit_id: int = Field(gt=0)
    purpose: str = Field(min_length=1, max_length=30)
    channel_chat_id: int
    thread_topic_id: int | None = None
    notes: str | None = Field(default=None, max_length=500)


class UnitChannelBindingCreate(UnitChannelBindingBase):
    pass


class UnitChannelBindingRead(BaseModel):
    id: int
    organization_id: int
    unit_id: int
    purpose: str
    channel_chat_id: int | None
    thread_topic_id: int | None
    status: str
    revoked_at: str | None = None
    revoked_by_membership_id: int | None = None
    notes: str | None
    created_at: str
    updated_at: str
    created_by: int | None = None
    updated_by: int | None = None
