from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.tenant import TenantContactStatus
from app.schemas.common import AuditFields


class TenantBase(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    secondary_phone: str | None = Field(default=None, max_length=50)
    telegram: str | None = Field(default=None, max_length=100)
    whatsapp: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=200)
    contact_status: TenantContactStatus | None = None
    nationality: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=1000)
    id_number: str | None = Field(default=None, max_length=100)
    id_front_file_id: str | None = Field(default=None, max_length=300)
    id_back_file_id: str | None = Field(default=None, max_length=300)
    emergency_name: str | None = Field(default=None, max_length=200)
    emergency_relationship: str | None = Field(default=None, max_length=100)
    emergency_phone: str | None = Field(default=None, max_length=50)
    is_active: bool = True


class TenantCreate(TenantBase):
    organization_id: int


class TenantUpdate(BaseModel):
    organization_id: int | None = None
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    secondary_phone: str | None = Field(default=None, max_length=50)
    telegram: str | None = Field(default=None, max_length=100)
    whatsapp: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=200)
    contact_status: TenantContactStatus | None = None
    nationality: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=1000)
    id_number: str | None = Field(default=None, max_length=100)
    id_front_file_id: str | None = Field(default=None, max_length=300)
    id_back_file_id: str | None = Field(default=None, max_length=300)
    emergency_name: str | None = Field(default=None, max_length=200)
    emergency_relationship: str | None = Field(default=None, max_length=100)
    emergency_phone: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None


class TenantPublic(BaseModel):
    """Safe tenant read: ID number is REDACTED (group / digest / archive-safe).

    Only ``id_registered`` (a boolean = ``ID：已登记``) and ``id_type`` are
    disclosed; the actual number and file ids are never exposed on this path.
    Create/update accept the raw fields, but reads only return the safe shape
    unless an explicit ``include_sensitive`` read is used.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    phone: str | None = None
    secondary_phone: str | None = None
    telegram: str | None = None
    whatsapp: str | None = None
    email: str | None = None
    contact_status: TenantContactStatus | None = None
    last_confirmed_at: datetime | None = None
    last_confirmed_by: str | None = None
    notes: str | None = None
    nationality: str | None = None
    emergency_name: str | None = None
    emergency_relationship: str | None = None
    emergency_phone: str | None = None
    is_active: bool = True
    id_registered: bool = False
    # created/updated audit (AuditFields subset that is always safe to share).
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TenantRead(TenantBase, AuditFields):
    """Full tenant read (manager/admin only). Callers that render to group or
    archive MUST NOT use this; use :class:`TenantPublic` + ``id_registered``."""

    id: int
    last_confirmed_at: datetime | None = None
    last_confirmed_by: str | None = None
