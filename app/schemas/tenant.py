from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import AuditFields


class TenantBase(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=200)
    nationality: str | None = Field(default=None, max_length=100)
    id_document: str | None = Field(default=None, max_length=200)
    emergency_contact: str | None = Field(default=None, max_length=200)
    is_active: bool = True


class TenantCreate(TenantBase):
    pass


class TenantUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=200)
    nationality: str | None = Field(default=None, max_length=100)
    id_document: str | None = Field(default=None, max_length=200)
    emergency_contact: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None


class TenantRead(TenantBase, AuditFields):
    id: int
