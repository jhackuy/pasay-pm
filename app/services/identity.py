"""Fail-closed identity and Telegram destination resolution."""
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.models.identity import (
    ApiCredential, CommunicationEndpoint, CredentialState, Principal, PrincipalType,
    SecurityEvent, TelegramIdentityBinding,
)
from app.models.user import User


def eligible_human(user: User) -> bool:
    return user.id != 14 and user.username.casefold() != "maria"


def bind_internal_audit(db: Session, principal_name: str) -> None:
    """Attribute subsequent audits to one explicit SYSTEM principal/credential."""
    from app.services.audit import audit_context
    row = (db.query(Principal, ApiCredential).join(ApiCredential)
        .filter(Principal.name == principal_name,
                Principal.principal_type == PrincipalType.SYSTEM,
                Principal.is_active.is_(True), ApiCredential.state == CredentialState.ACTIVE,
                ApiCredential.revoked_at.is_(None), ApiCredential.purpose == f"internal:{principal_name}")
        .one_or_none())
    if row is None:
        raise RuntimeError(f"active internal credential for SYSTEM principal {principal_name!r} is missing")
    principal, credential = row
    audit_context.set((principal.id, principal.id, credential.id, "internal"))


def resolve_telegram_human(db: Session, external_user_id: int) -> tuple[User, Principal]:
    if not isinstance(external_user_id, int) or external_user_id <= 0:
        raise LookupError("Telegram effective_user.id must be a positive integer")
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
    # Security denials must survive rollback of the rejected business action.
    # Use a short independent transaction when the referenced user is already
    # durable; fall back to the caller transaction for freshly-created rows.
    event = dict(event_type="telegram_destination_rejected", user_id=user_id,
                 channel="telegram", reason=reason)
    try:
        AuditSession = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
        with AuditSession.begin() as audit_db:
            audit_db.add(SecurityEvent(**event))
    except Exception:
        db.add(SecurityEvent(**event))
        db.flush()


def resolve_telegram_destination(db: Session, user_id: int) -> str:
    """Resolve exactly one endpoint; legacy is allowed only with no history."""
    user = db.get(User, user_id)
    principal = (db.query(Principal).filter(
        Principal.user_id == user_id, Principal.principal_type == PrincipalType.HUMAN).one_or_none())
    history = [] if principal is None else db.query(CommunicationEndpoint).filter(
        CommunicationEndpoint.human_principal_id == principal.id,
        CommunicationEndpoint.channel == "telegram").all()
    if history:
        valid = [e for e in history if e.is_active and e.verified_at is not None and e.revoked_at is None]
        if len(valid) == 1 and user is not None and user.is_active and principal.is_active:
            return valid[0].destination
        _security_event(db, user_id, "endpoint history exists but has no unique active verified endpoint")
        raise LookupError("Telegram destination is missing, ambiguous, inactive, unverified, or revoked")
    if user is not None and user.is_active and user.telegram_chat_id:
        return user.telegram_chat_id
    _security_event(db, user_id, "no endpoint history and no eligible legacy destination")
    raise LookupError("Telegram destination is unavailable")
