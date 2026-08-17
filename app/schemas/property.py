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
    # PASAY-AI-EMPLOYEE-FOUNDATION-007 §5: management contact + output.
    management_company: str | None = Field(default=None, max_length=200)
    management_office_phone: str | None = Field(default=None, max_length=50)
    management_contact_person: str | None = Field(default=None, max_length=200)
    management_email: str | None = Field(default=None, max_length=200)
    management_office_location: str | None = Field(default=None, max_length=300)
    operational_notes: str | None = None


class PropertyCreate(PropertyBase):
    pass


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


class UnitBase(BaseModel):
    property_id: int
    unit_number: str = Field(min_length=1, max_length=50)
    floor: str | None = Field(default=None, max_length=20)
    size_sqm: Decimal | None = Field(default=None, max_digits=10, decimal_places=2)
    monthly_rent: Decimal = money_field(gt=0)
    status: UnitStatus = UnitStatus.vacant
    is_active: bool = True
    # AI-OPS-FOUNDATION-001 §16: richer lifecycle state
    # (VACANT/PREPARING/LISTED/VIEWING/RESERVED/OCCUPIED/NOTICE_GIVEN/
    # MOVE_OUT/INSPECTION) — stored as VARCHAR, legacy enum untouched.
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
