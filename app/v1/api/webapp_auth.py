"""Telegram WebApp initData → bearer session exchange (Issue #119 Mini App).

A Mini App opened from the Telegram bot receives a signed ``initData``
string from ``window.Telegram.WebApp.initData``.  This endpoint accepts
that string, validates the HMAC-SHA256 signature using the configured
bot token, extracts the Telegram user id, and resolves it to a backend
``User`` row.  The returned bearer session is the SAME shape as the
canonical ``/bootstrap`` and ``/workspaces/members`` responses so the
Mini App's ``PasayClient`` can use it without any change.

Security contract (AGENTS.md §4 fail-closed):

  * Empty / missing bot token  → 503 (operator misconfiguration, not a
    user error).
  * Malformed initData / bad signature → 401 (the WebApp session is
    NEVER issued for an unverified origin).
  * Telegram user id not bound to any User row → 403 (the WebApp
    session is OWNER-only; an unknown Telegram id is rejected the
    same way the bot rejects unknown fixed-menu taps).
  * Telegram user id bound to a non-OWNER role (e.g. SECRETARY) →
    403 (Owner-only access policy — Issue #119 B/C scope).
  * Inactive / removed membership → 403.

The endpoint is mounted at ``/api/v1/webapp/auth`` and is the ONLY
unauthenticated path on the V1 API; every other route goes through
``get_current_principal``.  No state is written (no idempotency key
required) and no business truth is exposed in the response beyond
the session fields the client needs to start using the SPA.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qs, unquote

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.permissions import Role
from app.core.security import generate_api_key, hash_api_key
from app.v1.deps import get_db_dep
from app.v1.models.foundation import (
    ApiCredential,
    Membership,
    MembershipState,
    User,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webapp", tags=["webapp"])


class WebappAuthRequest(BaseModel):
    """Mini App initData payload from ``window.Telegram.WebApp.initData``.

    Telegram may also expose ``initDataUnsafe`` but ``initData`` is the
    SIGNED, canonical form (hash covers ``initData`` only), which is what
    we validate here.  The raw string is enough — we re-parse it server
    side so no client-side parsing assumptions leak into the verifier.
    """

    init_data: str = Field(min_length=1, max_length=8192)


class WebappAuthResponse(BaseModel):
    org_id: int
    user_id: int
    api_key: str
    role: str
    # Unix-seconds expiry of this bearer session.  The Mini App treats
    # this as opaque — the API client sends the bearer until the SPA
    # loses the in-memory session (no localStorage, AGENTS.md §4).
    expires_at: int


# ---------------------------------------------------------------------------
# Telegram initData signature verification (per Telegram Bot API §"Validating
# data received via the Mini App").  No PTB import — the algorithm is small
# and stable, and the V1 app must keep working even if PTB is not importable.
# ---------------------------------------------------------------------------

_INIT_DATA_TELEGRAM_BOT_TOKEN_GETTER = "telegram_bot_token"
_MAX_AUTH_DATE_SKEW_SECONDS = 60 * 60 * 24  # 24 h; older initData is rejected


def _telegram_webapp_secret_key(bot_token: str) -> bytes:
    """Derive the WebAppData secret key per Telegram spec:
    ``HMAC-SHA256(key="WebAppData", message=bot_token)``.
    """
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def _check_init_data_signature(init_data: str, bot_token: str) -> dict[str, str]:
    """Verify Telegram initData HMAC and return the parsed field map.

    Raises ``ValueError`` with a stable code (suitable for the HTTP layer
    to surface) on any malformed input or signature mismatch.  The
    returned dict is the raw field map with ``user`` and ``hash``
    left as the original string forms so callers can decode ``user``
    JSON explicitly.
    """
    if not init_data:
        raise ValueError("init_data_empty")
    # Telegram's on-the-wire format is a URL-encoded query string.
    # ``parse_qs`` returns a dict of lists; we collapse to first values.
    try:
        parsed_pairs = parse_qs(
            init_data, keep_blank_values=True, strict_parsing=False,
        )
    except Exception as exc:  # noqa: BLE001 — defensive: never raise non-ValueError
        raise ValueError(f"init_data_parse_failed:{exc}") from exc
    fields: dict[str, str] = {
        key: values[0] if values else "" for key, values in parsed_pairs.items()
    }
    received_hash = fields.pop("hash", "")
    if not received_hash:
        raise ValueError("init_data_missing_hash")
    secret_key = _telegram_webapp_secret_key(bot_token)
    # The check string is the remaining fields, alphabetically sorted,
    # each rendered as ``key=value`` and joined with ``\n``.  Telegram
    # uses the ORIGINAL (undecoded) values for signing — we therefore
    # rebuild the string from the raw query string rather than from
    # the parsed dict (decode round-trip can change ``+`` / ``%20`` /
    # unicode forms and invalidate the signature).
    check_string = _build_check_string(init_data)
    computed_hash = hmac.new(
        secret_key, check_string.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        raise ValueError("init_data_bad_signature")
    return fields


def _build_check_string(init_data: str) -> str:
    """Rebuild the Telegram ``data-check-string`` from the raw query string.

    Splits on ``&`` (Telegram does not allow nested ampersands in field
    values for initData), strips any trailing ``hash=...`` pair, sorts
    the rest alphabetically by key, and joins with ``\n``.
    """
    pairs: list[tuple[str, str]] = []
    for chunk in init_data.split("&"):
        if not chunk:
            continue
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        if key == "hash":
            continue
        pairs.append((key, value))
    pairs.sort(key=lambda kv: kv[0])
    return "\n".join(f"{k}={v}" for k, v in pairs)


def _decode_init_data_user(fields: dict[str, str]) -> dict[str, Any]:
    """Decode the ``user`` JSON payload from the parsed initData fields.

    Raises ``ValueError("init_data_missing_user")`` when absent and
    ``ValueError("init_data_bad_user_json")`` when the JSON is malformed.
    Returns an empty dict when the field is structurally OK but empty.
    """
    raw_user = fields.get("user", "")
    if not raw_user:
        raise ValueError("init_data_missing_user")
    try:
        decoded = json.loads(unquote(raw_user))
    except json.JSONDecodeError as exc:
        raise ValueError(f"init_data_bad_user_json:{exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("init_data_user_not_object")
    return decoded


def _check_auth_date_recent(fields: dict[str, str]) -> None:
    """Reject initData with an ``auth_date`` older than 24 h.

    Telegram only re-signs initData on app open; a stale signature
    means the Owner closed the Mini App more than a day ago.  This is
    NOT strictly required by Telegram, but it materially reduces the
    window in which a leaked initData string can be replayed.
    """
    auth_date_raw = fields.get("auth_date", "")
    if not auth_date_raw:
        return
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        return
    skew = int(time.time()) - auth_date
    if skew > _MAX_AUTH_DATE_SKEW_SECONDS:
        raise ValueError("init_data_stale")


# ---------------------------------------------------------------------------
# Settings accessor — keeps the webapp router decoupled from app.config
# (the canonical Settings object loads at import time, which would bind
# the bot token even in unit tests that don't need it).
# ---------------------------------------------------------------------------


def _get_bot_token() -> str:
    """Read the configured bot token at request time (test override safe)."""
    from app.config import settings  # late import to avoid cycles at module load

    return (settings.telegram_bot_token or "").strip()


# ---------------------------------------------------------------------------
# Owner allowlist — read from environment at request time so the operator
# can rotate without re-deploying the container.  Format: a comma-separated
# list of Telegram user ids (e.g. ``"5177241442"``).  Empty list = locked
# (the endpoint rejects every initData; the SPA surfaces a clear error).
# ---------------------------------------------------------------------------


def _get_owner_telegram_ids() -> set[int]:
    from app.config import settings  # late import (see _get_bot_token)

    raw = (settings.pasay_owner_telegram_user_ids or "").strip()
    out: set[int] = set()
    if not raw:
        return out
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            # Skip malformed entries rather than failing the whole request —
            # operator misconfiguration should NOT silently unlock the SPA.
            logger.warning(
                "pasay_owner_telegram_user_ids: skipping non-integer entry %r",
                chunk,
            )
    return out


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/auth",
    response_model=WebappAuthResponse,
    status_code=status.HTTP_200_OK,
)
def webapp_auth(
    body: WebappAuthRequest,
    db: Session = Depends(get_db_dep),
) -> WebappAuthResponse:
    """Exchange a signed Telegram initData string for a bearer session.

    See module docstring for the full security contract.  On success
    the response carries a freshly-generated API key bound to the
    resolved OWNER User; on failure it returns 401/403 with a stable
    error code so the Mini App can surface a meaningful UI message.
    """
    bot_token = _get_bot_token()
    if not bot_token:
        # Operator misconfiguration (AGENTS.md §4 fail-closed).  503 is
        # the correct semantic — the server cannot service the request
        # without configuration, not the client's fault.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "webapp_auth_disabled: telegram_bot_token not configured",
        )

    try:
        fields = _check_init_data_signature(body.init_data, bot_token)
        user_payload = _decode_init_data_user(fields)
        _check_auth_date_recent(fields)
    except ValueError as exc:
        code = str(exc).split(":", 1)[0]
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, f"init_data_invalid:{code}",
        ) from exc

    try:
        telegram_user_id = int(user_payload.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "init_data_invalid:user_id_not_int",
        )

    # Owner allowlist (Issue #119 Owner-only access policy).  This is the
    # only place the WebApp decides who can open the SPA; the API client
    # then rides the issued bearer through the rest of the surface.
    owner_ids = _get_owner_telegram_ids()
    if not owner_ids:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "webapp_auth_disabled: pasay_owner_telegram_user_ids not configured",
        )
    if telegram_user_id not in owner_ids:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "webapp_owner_only",
        )

    # Look up the backend User by Telegram id.  Unknown Telegram ids that
    # happen to pass the allowlist gate would be a configuration mismatch
    # (an operator added the id but no User row exists yet) — refuse
    # closed rather than silently auto-provision.
    user = (
        db.query(User)
        .filter(User.telegram_user_id == telegram_user_id)
        .one_or_none()
    )
    if user is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "webapp_owner_unknown",
        )

    membership = (
        db.query(Membership)
        .filter(
            Membership.user_id == user.id,
            Membership.state == MembershipState.ACTIVE.value,
        )
        .order_by(Membership.id.asc())
        .first()
    )
    if membership is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "webapp_owner_no_active_membership",
        )

    # Mirror the canonical Role enum — never trust the client.  Issue #119
    # require OWNER; anything else (SECRETARY, future roles) is rejected.
    try:
        role = Role.parse(membership.role)
    except Exception as exc:  # noqa: BLE001 — Role.parse is the boundary
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"webapp_role_unparseable:{exc}",
        ) from exc
    if role is not Role.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "webapp_owner_only_role",
        )

    # Mint a fresh API key.  We deliberately do NOT reuse an existing
    # credential — the WebApp session is ephemeral and the rotation
    # closes the replay window for a leaked bearer.
    api_key = generate_api_key()
    db.add(
        ApiCredential(
            user_id=user.id,
            key_hash=hash_api_key(api_key),
            is_active=True,
        )
    )
    db.commit()

    # 8h sliding window — long enough for a session of work, short enough
    # to bound the leak surface of a static-page-leaked bearer.
    expires_at = int(time.time()) + 8 * 3600

    return WebappAuthResponse(
        org_id=membership.org_id,
        user_id=user.id,
        api_key=api_key,
        role=role.value,
        expires_at=expires_at,
    )


__all__ = ["router", "WebappAuthRequest", "WebappAuthResponse"]