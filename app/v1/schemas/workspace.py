"""Workspace (Org) + Membership schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: datetime
    updated_at: datetime


class MembershipCreate(BaseModel):
    user_id: int = Field(gt=0)
    role: str = Field(pattern="^(owner|secretary|tenant)$")


class MembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    user_id: int
    role: str
    state: str