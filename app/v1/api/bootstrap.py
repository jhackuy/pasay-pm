"""Bootstrap API — dev/test only. Creates the first OWNER + API credential.

The bootstrap endpoint is UNAUTHENTICATED and only accepts requests when
no User exists in the database. After the first user is created, all
subsequent calls return 403. This is the canonical way tests acquire an
initial bearer token.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.security import generate_api_key, hash_api_key
from app.v1.deps import get_db_dep
from app.v1.models.foundation import (
    ApiCredential,
    Membership,
    MembershipState,
    Organization,
    Role,
    User,
)

router = APIRouter(prefix="/bootstrap", tags=["bootstrap"])


class BootstrapRequest(BaseModel):
    workspace_name: str = Field(min_length=1, max_length=120)
    owner_username: str | None = Field(default=None, max_length=64)
    owner_display_name: str | None = Field(default=None, max_length=120)


class BootstrapResponse(BaseModel):
    org_id: int
    user_id: int
    api_key: str
    role: str


@router.post(
    "", response_model=BootstrapResponse, status_code=status.HTTP_201_CREATED,
)
def bootstrap(
    body: BootstrapRequest, db: Session = Depends(get_db_dep),
) -> BootstrapResponse:
    # Refuse if any user already exists.
    existing_user = db.query(User).first()
    if existing_user is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "bootstrap disabled: users already exist",
        )
    user = User(
        telegram_id=None,
        username=body.owner_username,
        display_name=body.owner_display_name or body.owner_username,
    )
    db.add(user)
    db.flush()
    org = Organization(name=body.workspace_name)
    db.add(org)
    db.flush()
    membership = Membership(
        org_id=org.id,
        user_id=user.id,
        role=Role.OWNER.value,
        state=MembershipState.ACTIVE.value,
    )
    db.add(membership)
    api_key = generate_api_key()
    cred = ApiCredential(
        user_id=user.id,
        key_hash=hash_api_key(api_key),
        is_active=True,
    )
    db.add(cred)
    db.commit()
    return BootstrapResponse(
        org_id=org.id,
        user_id=user.id,
        api_key=api_key,
        role=Role.OWNER.value,
    )
