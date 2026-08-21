"""PASAY-TASK-006 Onboarding P0 — request / response schemas.

Owner language: Chinese-priority.
Secretary language: English-priority.
No i18n framework — only the contract-specified literal strings are embedded.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


OnboardingStage = Literal[
    "ROLE_CHOICE_REQUIRED",
    "OWNER_BOOTSTRAP_REQUIRED",
    "SECRETARY_NO_INVITE",
    "SECRETARY_VALID_INVITE_PENDING_ACCEPT",
    "EXISTING_MEMBER",
]


class OrganizationRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_name: str | None


class MembershipRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    role: str
    state: str


class OnboardingStateResponse(BaseModel):
    """Authoritative first-entry state for any caller.

    The frontend (Telegram bot / Mini App / Web) reads this endpoint ONCE
    per user entry and branches exactly on ``stage``. No UI assumptions
    are made here; the service only returns the semantic routing outcome +
    literal guidance strings for Secretary (English) / Owner (Chinese).
    """
    model_config = ConfigDict(from_attributes=True)

    stage: OnboardingStage
    user_id: int

    existing_organization: OrganizationRef | None = None
    existing_membership: MembershipRef | None = None

    owner_hint_zh: str | None = None
    secretary_hint_en: str | None = None

    invite_organization_name: str | None = None


class OwnerBootstrapRequest(BaseModel):
    org_name: str = Field(..., min_length=1, max_length=200)


class OwnerBootstrapResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization: OrganizationRef
    membership: MembershipRef


class SecretaryAcceptInviteRequest(BaseModel):
    invite_code: str = Field(..., min_length=1, max_length=128)


class SecretaryAcceptInviteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization: OrganizationRef
    membership: MembershipRef
