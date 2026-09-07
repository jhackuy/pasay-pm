"""FastAPI dependencies: auth (bearer API key), org-scope, idempotency helpers.

Bypasses the legacy `app/api/routers/` layer entirely — this is the V1
canonical auth path.

AGENTS.md §4: fail-closed. PermissionDenied → 403; UnknownRoleError → 400
(distinct from 401/403).

Issue #119 P0 v1_api_credential_bootstrap: adds SYSTEM-credential auth.

Two distinct auth surfaces, mutually exclusive:

1. ``get_current_principal`` — HUMAN membership auth.
   Resolves a Bearer token to a (user_id, org_id, role) Principal by
   requiring v1_memberships.state='ACTIVE'. Used by every interactive
   V1 endpoint. SYSTEM credentials are explicitly REJECTED here (a
   SYSTEM credential must NEVER impersonate a HUMAN).

2. ``get_system_principal`` — SYSTEM scheduled-job auth.
   Resolves a Bearer token to a SystemPrincipal carrying ONLY the
   credential row + the canonical purpose name. Does NOT resolve a
   user_id or org_id. Used by the read-only system endpoints (digest /
   next_check / reconcile) where a HUMAN bind would be wrong by
   construction. SYSTEM credentials cannot reach
   ``get_current_principal`` paths — that is the least-privilege
   guarantee (JOB-SERVICE-AUTH-002 carryover).
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
    SystemPrincipal,
    UnknownRoleError,
)
from app.core.security import hash_api_key
from app.db.session import get_db
from app.v1.models.base import (
    MembershipState,
    V1PrincipalType,
    V1_SYSTEM_PURPOSES,
)
from app.v1.models.foundation import ApiCredential, Membership


def get_db_dep(db: Session = Depends(get_db)) -> Session:
    """Re-export of get_db for module-level deps."""
    return db


def _extract_bearer(authorization: Optional[str]) -> str:
    """Extract the raw Bearer token from an Authorization header. 401 on miss.

    Empty / whitespace / non-Bearer / missing all collapse to 401; the
    caller never sees the difference so we don't leak header parsing.
    """
    if not authorization:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing bearer token",
        )
    # Use a manual split instead of ``str.split(None, 1)`` so we don't
    # IndexError on "Bearer " (trailing space, no token). Splitting on
    # the literal "bearer " prefix (case-insensitive) is also more
    # explicit about the contract we accept.
    lowered = authorization.lower()
    if not lowered.startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing bearer token",
        )
    raw_key = authorization[len("bearer "):].strip()
    if not raw_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "empty bearer token",
        )
    return raw_key


def _resolve_active_credential(
    db: Session, raw_key: str, *, principal_type: Optional[str] = None,
) -> ApiCredential:
    """Look up the active ApiCredential for ``raw_key``.

    When ``principal_type`` is given, the credential MUST carry that
    exact principal_type — used by ``get_system_principal`` to keep
    SYSTEM credentials out of HUMAN endpoints and vice versa.
    """
    key_hash = hash_api_key(raw_key)
    q = db.query(ApiCredential).filter(
        ApiCredential.key_hash == key_hash,
        ApiCredential.is_active.is_(True),
    )
    if principal_type is not None:
        q = q.filter(ApiCredential.principal_type == principal_type)
    cred = q.one_or_none()
    if cred is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid credentials",
        )
    return cred


def get_current_principal(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Principal:
    """Authenticate a Bearer API credential and return the Principal.

    Issue #119 P0 v1_api_credential_bootstrap: SYSTEM credentials are
    EXPLICITLY rejected here — a SYSTEM principal must never reach a
    HUMAN-membership-protected endpoint. This is the lock that keeps
    PASSAY_JOB_API_KEY from ever escalating to a write the Owner can
    see (JOB-SERVICE-AUTH-002 carryover). The 401 (not 403) is
    deliberate: a SYSTEM caller should look indistinguishable from any
    other unauthenticated caller; we never confirm whether the token
    exists at all.
    """
    raw_key = _extract_bearer(authorization)
    cred = _resolve_active_credential(db, raw_key)
    if cred.principal_type == V1PrincipalType.SYSTEM.value:
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


def get_system_principal(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> SystemPrincipal:
    """Authenticate a SYSTEM scheduled-job credential.

    Issue #119 P0 v1_api_credential_bootstrap: a SYSTEM credential
    (principal_type='SYSTEM', purpose='internal:scheduler') can ONLY
    reach this dependency. It does NOT resolve to a user_id / org_id
    / role: SYSTEM credentials carry no HUMAN identity and never bind
    a Telegram id. Endpoints that accept a SystemPrincipal must
    themselves enforce read-only / org-scope semantics — see
    ``app.v1.api.system_ops`` for the canonical pattern.

    Failure modes (fail closed):
    - Missing / malformed Authorization header → 401 ``invalid credentials``.
    - Active HUMAN credential → 401 ``invalid credentials`` (never
      confirm whether the credential exists).
    - Active SYSTEM credential with an unknown purpose → 401
      ``invalid credentials``.
    - Inactive / revoked SYSTEM credential → 401 ``invalid credentials``.
    """
    raw_key = _extract_bearer(authorization)
    cred = _resolve_active_credential(
        db, raw_key, principal_type=V1PrincipalType.SYSTEM.value,
    )
    if cred.purpose not in V1_SYSTEM_PURPOSES:
        # Fail closed: an active SYSTEM credential with an unknown /
        # mistyped purpose is indistinguishable from a forged one.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid credentials",
        )
    return SystemPrincipal(credential=cred, name=cred.purpose)


def require_role(*allowed: Role):
    """Dependency factory: enforce principal.role ∈ allowed set.

    SYSTEM credentials (SystemPrincipal) are NEVER accepted by
    ``require_role``: a SYSTEM caller has no role, so the
    ``principal.role not in allowed_set`` test fails closed. This is
    the second leg of the SYSTEM-cannot-write guarantee
    (JOB-SERVICE-AUTH-002 carryover) — even if a future route forgets
    to use ``get_system_principal``, a SYSTEM credential cannot pass
    ``require_role(OWNER)`` because ``isinstance(principal, Principal)``
    is False for SystemPrincipal.
    """
    allowed_set = set(allowed)

    def dep(
        principal: Principal = Depends(get_current_principal),
    ) -> Principal:
        if not isinstance(principal, Principal):
            # SystemPrincipal hit this dep — fail closed.
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"system credential not accepted by require_role",
            )
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
    "get_system_principal",
    "get_db_dep",
    "parse_idempotency_key_header",
    "Principal",
    "SystemPrincipal",
    "require_role",
    "compute_payload_hash",
]