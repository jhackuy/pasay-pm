"""Fail-closed identity and Telegram destination resolution."""
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.models.identity import (
    ApiCredential, CommunicationEndpoint, CredentialState, Principal, PrincipalType,
    SecurityEvent, TelegramIdentityBinding,
)
from app.models.user import User

MAX_TELEGRAM_USER_ID = 2**63 - 1
INTERNAL_SYSTEM_PRINCIPALS = frozenset({"scheduler", "reconcile", "notifier", "backfill"})


def eligible_human(user: User) -> bool:
    return bool(
        user.is_active
        and user.id != 14
        and user.username.casefold() != "maria"
    )


def normalize_telegram_user_id(value: int | str) -> int:
    """Return one canonical positive BIGINT Telegram ``effective_user.id``.

    Bot-generated headers are canonical decimal strings. Whitespace, signs,
    leading zeroes, booleans, group chat ids, and out-of-range values are
    rejected instead of being silently normalized into another identity.
    """
    if isinstance(value, bool):
        raise LookupError("Telegram effective_user.id must be a positive integer")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and value and value.isascii() and value.isdigit():
        if value.startswith("0"):
            raise LookupError("Telegram effective_user.id is not canonical")
        normalized = int(value)
    else:
        raise LookupError("Telegram effective_user.id must be a positive integer")
    if normalized <= 0 or normalized > MAX_TELEGRAM_USER_ID:
        raise LookupError("Telegram effective_user.id must be a positive BIGINT")
    return normalized


def bind_internal_audit(db: Session, principal_name: str) -> None:
    """Attribute subsequent audits to one explicit SYSTEM principal/credential."""
    from app.services.audit import set_audit_context
    if principal_name not in INTERNAL_SYSTEM_PRINCIPALS:
        raise RuntimeError(f"unknown internal SYSTEM principal {principal_name!r}")
    row = (db.query(Principal, ApiCredential).join(ApiCredential)
        .filter(Principal.name == principal_name,
                Principal.principal_type == PrincipalType.SYSTEM,
                Principal.is_active.is_(True), ApiCredential.state == CredentialState.ACTIVE,
                ApiCredential.revoked_at.is_(None), ApiCredential.purpose == f"internal:{principal_name}")
        .one_or_none())
    if row is None:
        raise RuntimeError(f"active internal credential for SYSTEM principal {principal_name!r} is missing")
    principal, credential = row
    set_audit_context(
        db, (principal.id, principal.id, credential.id, "internal")
    )


def resolve_telegram_human(db: Session, external_user_id: int) -> tuple[User, Principal]:
    external_user_id = normalize_telegram_user_id(external_user_id)
    rows = (
        db.query(User, Principal)
        .join(Principal, Principal.user_id == User.id)
        .join(TelegramIdentityBinding,
              TelegramIdentityBinding.human_principal_id == Principal.id)
        .filter(
            TelegramIdentityBinding.external_user_id == external_user_id,
            TelegramIdentityBinding.is_active.is_(True),
            TelegramIdentityBinding.verified_at.is_not(None),
            TelegramIdentityBinding.revoked_at.is_(None),
            Principal.principal_type == PrincipalType.HUMAN,
            Principal.is_active.is_(True), User.is_active.is_(True),
            User.id != 14, func.lower(User.username) != "maria",
        ).all()
    )
    if len(rows) != 1:
        raise LookupError("Telegram identity is not uniquely verified")
    return rows[0]


def _security_event(db: Session, user_id: int | None, reason: str) -> None:
    """Commit a destination denial independently of the rejected transaction."""
    AuditSession = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    with AuditSession.begin() as audit_db:
        # A missing or freshly-created user cannot satisfy the FK in this
        # independent transaction. Keep the requested id in the reason while
        # using the FK only when the user is already durable and visible.
        durable_user_id = (
            user_id
            if user_id is not None and audit_db.get(User, user_id) is not None
            else None
        )
        audit_db.add(SecurityEvent(
            event_type="telegram_destination_rejected",
            user_id=durable_user_id,
            channel="telegram",
            reason=f"requested_user_id={user_id!r}; {reason}",
        ))


def _reject_destination(db: Session, user_id: int | None, reason: str) -> None:
    _security_event(db, user_id, reason)
    raise LookupError("Telegram destination is missing, ambiguous, inactive, unverified, or revoked")


def resolve_telegram_destination(db: Session, user_id: int) -> str:
    """Resolve exactly one endpoint; legacy is allowed only with no history."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        _reject_destination(db, None, "canonical assignee id is not a positive integer")
    user = db.get(User, user_id)
    if user is None:
        _reject_destination(db, user_id, "canonical assignee does not exist")
    if not eligible_human(user):
        _reject_destination(db, user_id, "canonical assignee is not an active eligible HUMAN")

    principals = (db.query(Principal).filter(
        Principal.user_id == user_id,
        Principal.principal_type == PrincipalType.HUMAN,
    ).all())
    if len(principals) > 1:
        _reject_destination(db, user_id, "canonical assignee has duplicate HUMAN principals")
    principal = principals[0] if principals else None
    if principal is not None and not principal.is_active:
        _reject_destination(db, user_id, "canonical HUMAN principal is inactive")
    history = [] if principal is None else db.query(CommunicationEndpoint).filter(
        CommunicationEndpoint.human_principal_id == principal.id,
        CommunicationEndpoint.channel == "telegram").all()
    if history:
        valid = [
            endpoint for endpoint in history
            if endpoint.is_active
            and endpoint.verified_at is not None
            and endpoint.revoked_at is None
            and endpoint.destination.strip()
        ]
        if len(valid) == 1:
            return valid[0].destination.strip()
        _reject_destination(
            db, user_id,
            "endpoint history exists but has no unique active verified endpoint",
        )
    legacy_destination = (user.telegram_chat_id or "").strip()
    if legacy_destination:
        return legacy_destination
    _reject_destination(db, user_id, "no endpoint history and no eligible legacy destination")
