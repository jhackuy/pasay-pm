"""Safe default-assignee validation (V1.2 production data-hardening).

The fallback assignee (``OPERATIONS_DEFAULT_ASSIGNEE`` / ``DEFAULT_ASSIGNED_USER_ID``)
is the recipient for proactive notifications on business-source tasks that have no
explicit owner. A misconfigured default (missing user, inactive user, wrong role, or a
user with no registered Telegram chat id) would silently create un-notifiable tasks, so
we fail FAST at every use site instead of degrading silently to a broken board.

The ``telegram_chat_id`` requirement mirrors ``resolve_recipient``: a default assignee
without a chat id can never receive the notifications the system generates for them.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.user import User, UserRole

# Roles allowed to own proactive business tasks (admin / manager).
_ALLOWED_DEFAULT_ASSIGNEE_ROLES: frozenset[str] = frozenset(
    {UserRole.admin.value, UserRole.manager.value}
)


def validate_default_assignee(db: Session, user_id: int | None) -> User:
    """Return the default assignee User, raising a clear RuntimeError when invalid.

    Guards (fail-fast, never silent):

    1. ``user_id`` is None / not an int -> misconfigured env.
    2. user does not exist.
    3. user.is_active is False.
    4. user.role not in {admin, manager}.
    5. user.telegram_chat_id is falsy — the admin cannot receive the proactive
       notifications this default assignee is meant to receive.

    Raises:
        RuntimeError: with an actionable message describing the exact misconfiguration.
    """
    if user_id is None:
        raise RuntimeError(
            "OPERATIONS_DEFAULT_ASSIGNEE is not set (DEFAULT_ASSIGNED_USER_ID=None). "
            "Set it to the id of an active admin/manager user who has a registered "
            "Telegram chat id so proactive notifications have a valid recipient."
        )
    user = db.get(User, user_id)
    if user is None:
        raise RuntimeError(
            f"OPERATIONS_DEFAULT_ASSIGNEE={user_id} is invalid: no user with this id "
            "exists. Point it at an active admin/manager user and re-run."
        )
    if not user.is_active:
        raise RuntimeError(
            f"OPERATIONS_DEFAULT_ASSIGNEE={user_id} is invalid: user "
            f"'{user.username}' is inactive. Reactivate them or pick another "
            "admin/manager."
        )
    if user.role.value not in _ALLOWED_DEFAULT_ASSIGNEE_ROLES:
        raise RuntimeError(
            f"OPERATIONS_DEFAULT_ASSIGNEE={user_id} is invalid: user "
            f"'{user.username}' has role '{user.role.value}'; the default assignee "
            f"must be an admin or manager (got one of "
            f"{sorted(_ALLOWED_DEFAULT_ASSIGNEE_ROLES)})."
        )
    if not user.telegram_chat_id:
        raise RuntimeError(
            f"OPERATIONS_DEFAULT_ASSIGNEE={user_id} is invalid: user "
            f"'{user.username}' has no Telegram chat id, so they can never receive "
            "the proactive notifications the system generates. Register "
            "telegram_chat_id for this user or pick a different admin/manager."
        )
    return user
