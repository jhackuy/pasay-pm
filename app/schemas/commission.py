from decimal import Decimal

from pydantic import BaseModel, Field

from app.models import AgentRole, CommissionRuleType, CommissionSettlementStatus
from app.schemas.common import AuditFields, money_field


class CommissionRuleBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rule_type: CommissionRuleType
    value: Decimal = money_field(gt=0)
    agent_role: AgentRole
    is_active: bool = True


class CommissionRuleCreate(CommissionRuleBase):
    pass


class CommissionRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    rule_type: CommissionRuleType | None = None
    value: Decimal | None = money_field(gt=0, default=None)
    agent_role: AgentRole | None = None
    is_active: bool | None = None


class CommissionRuleRead(CommissionRuleBase, AuditFields):
    id: int


class CommissionSettlementCreate(BaseModel):
    agent_id: int
    lease_id: int
    rule_id: int
    notes: str | None = None


class CommissionSettlementRead(CommissionSettlementCreate, AuditFields):
    id: int
    computed_amount: Decimal = money_field(ge=0)
    status: CommissionSettlementStatus
