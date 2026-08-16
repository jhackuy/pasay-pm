from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import hash_api_key
from app.database import get_db
from app.models.identity import ApiCredential, CredentialState, Principal, PrincipalType
from app.models.user import User, UserRole
from app.services.audit import current_audit_context, set_audit_context
from app.services.identity import (
    eligible_human,
    normalize_telegram_user_id,
    resolve_telegram_human,
)

bearer_scheme = HTTPBearer(auto_error=False)

# JOB-SERVICE-AUTH-002: background proactive jobs (v2_daily_digest /
# v2_next_check in pasay-telegram-bot/pasay_bot/jobs.py) authenticate as a real
# SYSTEM principal with its own internal credential — they NEVER impersonate the
# Owner's Telegram id. Only this one SYSTEM principal + purpose pair is accepted
# for the read-only operations endpoints, and only WITHOUT X-Telegram-User-Id
# (a SYSTEM credential must never present a Telegram subject).
SYSTEM_JOB_READER_PRINCIPAL = "scheduler"
SYSTEM_JOB_READER_PURPOSE = "internal:scheduler"
SYSTEM_JOB_READER_CHANNEL = "internal"


class SystemReader:
    """SYSTEM scheduled-job subject authorized for read-only operations reads.

    Carries the authenticated SYSTEM principal/credential so audit provenance
    is unambiguous (subject = caller = the SYSTEM principal, never a HUMAN).
    The service layer reads ``role``/``id``: ``role`` is pinned to the global
    read scope (all active tasks, same payload the Owner-scoped jobs received
    before JOB-SERVICE-AUTH-002) and ``id`` is None because there is NO HUMAN
    user behind a SYSTEM job. This object is returned ONLY by
    :func:`get_operations_reader` and is never accepted by
    ``get_current_user`` / ``owner_subject_only`` / role gates, so a SYSTEM
    credential can never reach a write endpoint.
    """

    __slots__ = ("principal", "credential")

    def __init__(self, principal: Principal, credential: ApiCredential):
        self.principal = principal
        self.credential = credential

    role = UserRole.admin  # global read scope (all active tasks)
    id = None  # no HUMAN user behind a SYSTEM job


def _resolve_current_user(
    credentials: HTTPAuthorizationCredentials | None,
    x_telegram_user_id: str | None,
    db: Session,
) -> User:
    """Resolve one canonical HUMAN subject and bind request provenance."""
    # A failed request must not leave provenance from an earlier request in a
    # recycled async context.
    set_audit_context(db, (None, None, None, None))
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    key_hash = hash_api_key(credentials.credentials)
    record = (db.query(ApiCredential, Principal)
        .join(Principal, Principal.id == ApiCredential.principal_id)
        .filter(ApiCredential.key_hash == key_hash).one_or_none())
    if record is not None:
        credential, caller = record
        if credential.state != CredentialState.ACTIVE or credential.revoked_at is not None or not caller.is_active:
            raise HTTPException(status_code=401, detail="Revoked or inactive credential")
        if caller.principal_type == PrincipalType.SERVICE:
            if caller.name != "native-bot" or credential.purpose != "telegram_bot" or x_telegram_user_id is None:
                raise HTTPException(status_code=401, detail="Telegram Bot credential requires its purpose and X-Telegram-User-Id")
            try:
                user, subject = resolve_telegram_human(
                    db, normalize_telegram_user_id(x_telegram_user_id)
                )
            except LookupError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            set_audit_context(
                db, (subject.id, caller.id, credential.id, "telegram")
            )
            return user
        if caller.principal_type == PrincipalType.HUMAN:
            if x_telegram_user_id is not None:
                raise HTTPException(status_code=401, detail="Human credentials cannot delegate a Telegram subject")
            if caller.user_id is None:
                raise HTTPException(status_code=401, detail="Human principal has no canonical user")
            user = db.get(User, caller.user_id)
            if user is None or not user.is_active or not eligible_human(user):
                raise HTTPException(status_code=401, detail="Ineligible human identity")
            set_audit_context(db, (caller.id, caller.id, credential.id, "api"))
            return user
        raise HTTPException(status_code=401, detail="Credential principal cannot authenticate a human endpoint")
    user = (
        db.query(User)
        .filter(User.api_key_hash == key_hash, User.is_active.is_(True))
        .first()
    )
    if user is None or not eligible_human(user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if x_telegram_user_id is not None:
        raise HTTPException(status_code=401, detail="Human credentials cannot delegate a Telegram subject")
    # Compatibility read path for canonical HUMAN key hashes. The V1.3
    # migration creates the principal; a missing, inactive, or ambiguous
    # canonical principal is not a valid human identity.
    principals = db.query(Principal).filter(
        Principal.user_id == user.id,
        Principal.principal_type == PrincipalType.HUMAN,
    ).all()
    if len(principals) != 1 or not principals[0].is_active:
        raise HTTPException(
            status_code=401,
            detail="Missing, inactive, or ambiguous human principal",
        )
    principal = principals[0]
    set_audit_context(
        db,
        (
            principal.id,
            principal.id,
            None,
            "api",
        ),
    )
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_telegram_user_id: str | None = Header(default=None, alias="X-Telegram-User-Id"),
    db: Session = Depends(get_db),
) -> User:
    return _resolve_current_user(credentials, x_telegram_user_id, db)


def get_operations_reader(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_telegram_user_id: str | None = Header(default=None, alias="X-Telegram-User-Id"),
    db: Session = Depends(get_db),
) -> User | SystemReader:
    """Authorize one caller for the deterministic read-only operations reads.

    JOB-SERVICE-AUTH-002: a real SYSTEM principal (``scheduler``) with its
    active internal credential may read the deterministic operations endpoints
    AS ITSELF — no X-Telegram-User-Id, no human resolution, no Owner fallback.
    Provenance is bound to the SYSTEM principal (channel ``internal``).

    Every other caller (HUMAN credentials, the native-bot SERVICE credential
    with a verified Telegram subject) is resolved exactly as before via
    ``_resolve_current_user`` — Owner / Secretary behavior is unchanged. This
    dependency is used ONLY on read-only operations endpoints; write and
    role-gated endpoints still use ``get_current_user`` / ``owner_subject_only``,
    where a SYSTEM credential is rejected, so it can never escalate to an
    Owner-only write.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    key_hash = hash_api_key(credentials.credentials)
    record = (
        db.query(ApiCredential, Principal)
        .join(Principal, Principal.id == ApiCredential.principal_id)
        .filter(ApiCredential.key_hash == key_hash)
        .one_or_none()
    )
    if record is not None:
        credential, caller = record
        if (
            caller.principal_type == PrincipalType.SYSTEM
            and caller.name == SYSTEM_JOB_READER_PRINCIPAL
            and credential.purpose == SYSTEM_JOB_READER_PURPOSE
            and credential.state == CredentialState.ACTIVE
            and credential.revoked_at is None
            and caller.is_active
            and x_telegram_user_id is None
        ):
            set_audit_context(
                db, (caller.id, caller.id, credential.id, SYSTEM_JOB_READER_CHANNEL)
            )
            return SystemReader(principal=caller, credential=credential)
    # All other identities follow the unchanged HUMAN / native-bot+Telegram path.
    return _resolve_current_user(credentials, x_telegram_user_id, db)


def owner_subject_only(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_telegram_user_id: str | None = Header(default=None, alias="X-Telegram-User-Id"),
    db: Session = Depends(get_db),
) -> User:
    """Authorize an active canonical HUMAN Owner, failing closed with 403.

    Owner-only transitions deliberately do not authorize the credential owner
    or SERVICE caller. ``_resolve_current_user`` first resolves the current
    HUMAN subject (including an exact active Telegram binding for Native Bot
    calls); this dependency then checks that subject's canonical role and
    provenance. Authentication failures are intentionally collapsed to 403
    at this authorization boundary.
    """
    try:
        user = _resolve_current_user(credentials, x_telegram_user_id, db)
    except HTTPException as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner authorization required",
        ) from exc

    subject_principal_id, _, _, _ = current_audit_context(db)
    subject = (
        db.get(Principal, subject_principal_id)
        if subject_principal_id is not None
        else None
    )
    if (
        user.role != UserRole.admin
        or subject is None
        or subject.principal_type != PrincipalType.HUMAN
        or subject.user_id != user.id
        or not subject.is_active
    ):
        # Do not retain provenance for a denied subject in a request-scoped
        # session that may be reused by direct tests or non-ASGI callers.
        set_audit_context(db, (None, None, None, None))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner authorization required",
        )
    return user


def require_roles(*roles: UserRole):
    """Dependency factory: restrict access to the given roles."""

    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
            )
        return user

    return checker


admin_only = require_roles(UserRole.admin)
manager_or_admin = require_roles(UserRole.admin, UserRole.manager)
