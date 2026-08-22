from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.move_out import MoveOutInspectionStatus
from app.schemas.common import AuditFields, money_field


class FindingsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str
    description: str | None = None
    severity: str | None = None
    cost: Decimal | None = money_field(ge=0, default=None)


class MoveOutInspectionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduled_at: datetime
    inspected_at: datetime | None = None
    findings: list[dict] | None = None
    evidence_ids: list[int] | None = None


class MoveOutInspectionCreate(MoveOutInspectionBase):
    model_config = ConfigDict(extra="forbid")
    lease_id: int
    notes: str | None = None


class MoveOutInspectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduled_at: datetime | None = None
    inspected_at: datetime | None = None
    findings: list[dict] | None = None
    evidence_ids: list[int] | None = None
    notes: str | None = None


class MoveOutInspectionConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pass


class MoveOutInspectionRead(MoveOutInspectionBase, AuditFields):
    model_config = ConfigDict(extra="allow")
    id: int
    lease_id: int
    unit_id: int
    tenant_id: int
    status: MoveOutInspectionStatus
    confirmed_at: datetime | None = None
