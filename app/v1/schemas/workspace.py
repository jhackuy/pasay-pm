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
    role: str = Field(pattern="^(OWNER|SECRETARY)$")


class MembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    user_id: int
    role: str
    state: str


class SecretaryInviteCreate(BaseModel):
    invitee_username: str | None = Field(default=None, max_length=64)
    invitee_telegram_id: int | None = Field(default=None, gt=0)


class SecretaryInviteAccept(BaseModel):
    invite_token: str = Field(min_length=1, max_length=64)
    accepting_user_id: int = Field(gt=0)


class SecretaryInviteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    invite_token: str
    invitee_username: str | None
    invitee_telegram_id: int | None
    role: str
    state: str
    expires_at: datetime
    accepted_at: datetime | None
    accepted_by_user_id: int | None