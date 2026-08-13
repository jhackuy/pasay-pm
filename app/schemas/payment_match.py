"""Request/response schemas for the read-only rent-payment matcher (Slice 2).

Only human-safe fields are exposed to the bot; raw enums / DB state stay
internal. ``kind``/``confidence`` are enum VALUES (open/pending/duplicate and
high/medium/low) and are rendered into human text by the bot, never shown
verbatim.
"""
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.common import money_field


class PaymentMatchRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    amount: Decimal | None = money_field(default=None, gt=0)


class PaymentMatchCandidate(BaseModel):
    kind: str
    confidence: str
    lease_id: int
    unit_id: int
    unit_number: str
    property_id: int
    property_name: str
    tenant_id: int
    tenant_name: str
    period: str
    due_date: date | None = None
    amount: Decimal
    open_count: int = 0
    due_amount: Decimal = Decimal("0.00")
    paid_amount: Decimal = Decimal("0.00")
    remaining_balance: Decimal = Decimal("0.00")
    income_id: int | None = None
    income_status: str | None = None


class PaymentMatchResponse(BaseModel):
    received_date: date
    candidates: list[PaymentMatchCandidate]
