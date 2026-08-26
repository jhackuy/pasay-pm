"""Minimal i18n module: role-based override + Accept-Language fallback.

No external libraries. Locale resolution order:
    1. Role override (OWNER -> "zh"; SECRETARY / agent -> "en")
    2. Accept-Language request header (only "zh" or "en" recognized)
    3. Hard fallback "en"
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database import get_db
from app.models.membership import Membership, MembershipState, OrganizationRole
from app.models.user import User

SUPPORTED_LOCALES = {"zh", "en"}
DEFAULT_LOCALE = "en"

MESSAGES: dict[str, dict[str, str]] = {
    "401_unauthorized": {
        "zh": "未授权",
        "en": "Unauthorized",
    },
    "403_no_permission": {
        "zh": "无权限",
        "en": "No permission",
    },
    "404_not_found": {
        "zh": "未找到",
        "en": "Not found",
    },
    "409_conflict": {
        "zh": "状态冲突",
        "en": "Conflict",
    },
    "422_unprocessable": {
        "zh": "请求参数无效",
        "en": "Unprocessable entity",
    },
    "org_required": {
        "zh": "需要组织上下文",
        "en": "Organization context required",
    },
}


def _normalize(locale_candidate: str | None) -> str:
    if not locale_candidate:
        return DEFAULT_LOCALE
    low = locale_candidate.strip().lower()
    if low.startswith("zh"):
        return "zh"
    if low.startswith("en"):
        return "en"
    return DEFAULT_LOCALE


def parse_accept_language(header_value: str | None) -> str:
    """Return first supported locale from an Accept-Language header, else DEFAULT_LOCALE."""
    if not header_value:
        return DEFAULT_LOCALE
    segments = [seg.strip() for seg in header_value.split(",") if seg.strip()]
    for seg in segments:
        tag = seg.split(";", 1)[0].strip()
        candidate = _normalize(tag)
        if candidate in SUPPORTED_LOCALES:
            return candidate
    return DEFAULT_LOCALE


def role_override(
    membership_role: OrganizationRole | None,
    user_role_tier: str | None = None,
) -> str | None:
    """Return role-based locale override or None if role info is missing."""
    if membership_role == OrganizationRole.OWNER:
        return "zh"
    if membership_role == OrganizationRole.SECRETARY:
        return "en"
    if user_role_tier is not None:
        tier = str(user_role_tier).lower()
        if tier in {"agent", "manager", "admin"}:
            return "en"
    return None


def resolve_locale(
    membership_role: OrganizationRole | None = None,
    user_role_tier: str | None = None,
    accept_language_header: str | None = None,
) -> str:
    override = role_override(membership_role, user_role_tier)
    if override:
        return override
    return parse_accept_language(accept_language_header)


def t(key: str, locale: str = DEFAULT_LOCALE) -> str:
    """Translate a message key. Falls back to en then raw key."""
    table = MESSAGES.get(key)
    if not table:
        return key
    normalized = _normalize(locale)
    if normalized in table:
        return table[normalized]
    if "en" in table:
        return table["en"]
    return next(iter(table.values()), key)


async def get_locale(
    request: Request,
    accept_language: Annotated[str | None, Header(include_in_schema=False)] = None,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
) -> str:
    """FastAPI dependency that resolves locale via role + Accept-Language.

    Role lookup uses the user's FIRST active membership (alphabetical by org id).
    Routers that already have a specific membership can call resolve_locale()
    directly with that membership.role instead of this dependency.
    """
    if user is None:
        return parse_accept_language(accept_language)

    membership = (
        db.query(Membership)
        .filter(
            Membership.user_id == user.id,
            Membership.state == MembershipState.ACTIVE,
            Membership.removed_at.is_(None),
        )
        .order_by(Membership.organization_id.asc())
        .first()
    )
    role = membership.role if membership is not None else None
    tier = getattr(user, "role", None)
    return resolve_locale(
        membership_role=role,
        user_role_tier=str(tier) if tier is not None else None,
        accept_language_header=accept_language,
    )
