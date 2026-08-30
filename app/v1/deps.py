"""FastAPI dependencies: auth (bearer API key), org-scope, idempotency helpers.

Bypasses the legacy `app/api/routers/` layer entirely — this is the V1
canonical auth path.

AGENTS.md §4: fail-closed. PermissionDenied → 403; UnknownRoleError → 400
(distinct from 401/403).
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.idempotency import (
    IdempotencyConflictError,
    IdempotencyKeyError,
    MAX_IDEMPOTENCY_KEY_LEN,
    compute_payload_hash,
    normalize_idempotency_key,
)
from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    UnknownRoleError,
)
from app.core.security import hash_api_key
from app.db.session import get_db
from app.v1.models.foundation import ApiCredential, Membership
from app.v1.models.base import MembershipState


def get_db_dep(db: Session = Depends(get_db)) -> Session:
    """Re-export of get_db for module-level deps."""
    return db


def get_current_principal(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Principal:
    """Authenticate a Bearer API credential and return the Principal."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing bearer token",
        )
    raw_key = authorization.split(None, 1)[1].strip()
    if not raw_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "empty bearer token",
        )
    key_hash = hash_api_key(raw_key)
    cred = (
        db.query(ApiCredential)
        .filter(
            ApiCredential.key_hash == key_hash,
            ApiCredential.is_active.is_(True),
        )
        .one_or_none()
    )
    if cred is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid credentials",
        )
    membership = (
        db.query(Membership)
        .filter(
            Membership.user_id == cred.user_id,
            Membership.state == MembershipState.ACTIVE.value,
        )
        .order_by(Membership.id.asc())
        .first()
    )
    if membership is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "user has no active membership",
        )
    try:
        role = Role.parse(membership.role)
    except UnknownRoleError as exc:
        # Parse failure is BAD REQUEST (400), NOT 401/403.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, str(exc),
        ) from exc
    return Principal(
        user_id=cred.user_id,
        org_id=membership.org_id,
        role=role,
        membership_state=membership.state,
    )


def require_role(*allowed: Role):
    """Dependency factory: enforce principal.role ∈ allowed set."""
    allowed_set = set(allowed)

    def dep(
        principal: Principal = Depends(get_current_principal),
    ) -> Principal:
        if principal.role not in allowed_set:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role {principal.role.value} not in "
                f"{sorted(r.value for r in allowed_set)}",
            )
        return principal

    return dep


def parse_idempotency_key_header(
    idempotency_key: Optional[str] = Header(
        None, alias="Idempotency-Key", max_length=MAX_IDEMPOTENCY_KEY_LEN + 1024,
    ),
) -> Optional[str]:
    """Parse an `Idempotency-Key` header. Returns normalized key or raises 400.

    The Idempotency-Key header is OPTIONAL. When present, it MUST pass
    `normalize_idempotency_key` (case-preserving, length-bounded, no
    silent truncation). When absent, returns None and the route handler
    decides whether to require it.
    """
    if idempotency_key is None:
        return None
    try:
        return normalize_idempotency_key(idempotency_key)
    except (IdempotencyConflictError, IdempotencyKeyError) as exc:
        # Both parse-level (oversize, empty, whitespace) and conflict-level
        # (already in a state that conflicts with this header) failures are
        # 400 Bad Request from the client's perspective.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, str(exc),
        ) from exc


__all__ = [
    "get_current_principal",
    "get_db_dep",
    "parse_idempotency_key_header",
    "Principal",
    "require_role",
    "compute_payload_hash",
]