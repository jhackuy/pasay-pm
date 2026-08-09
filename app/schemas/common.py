from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AuditFields(ORMModel):
    created_at: datetime
    updated_at: datetime
    created_by: int | None = None
    updated_by: int | None = None


def money_field(**kwargs) -> Any:
    """Shared Numeric(14,2) validation for money fields."""
    return Field(max_digits=14, decimal_places=2, **kwargs)


class MessageResponse(BaseModel):
    detail: str
