from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import hash_api_key
from app.database import get_db
from app.models.identity import ApiCredential, CredentialState, Principal, PrincipalType
from app.models.user import User, UserRole
from app.services.audit import audit_context
from app.services.identity import eligible_human, resolve_telegram_human

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_telegram_user_id: str | None = Header(default=None, alias="X-Telegram-User-Id"),
    db: Session = Depends(get_db),
) -> User:
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
            if credential.purpose != "telegram_bot" or x_telegram_user_id is None:
                raise HTTPException(status_code=401, detail="Telegram Bot credential requires its purpose and X-Telegram-User-Id")
            try:
                user, subject = resolve_telegram_human(db, int(x_telegram_user_id))
            except (ValueError, LookupError) as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            audit_context.set((subject.id, caller.id, credential.id, "telegram"))
            return user
        if caller.principal_type == PrincipalType.HUMAN:
            if x_telegram_user_id is not None:
                raise HTTPException(status_code=401, detail="Human credentials cannot delegate a Telegram subject")
            if caller.user_id is None:
                raise HTTPException(status_code=401, detail="Human principal has no canonical user")
            user = db.get(User, caller.user_id)
            if user is None or not user.is_active or not eligible_human(user):
                raise HTTPException(status_code=401, detail="Ineligible human identity")
            audit_context.set((caller.id, caller.id, credential.id, "api"))
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
    # Compatibility read path for canonical human keys only.
    principal = db.query(Principal).filter(Principal.user_id == user.id,
        Principal.principal_type == PrincipalType.HUMAN).one_or_none()
    audit_context.set((principal.id if principal else None, principal.id if principal else None, None, "api"))
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
