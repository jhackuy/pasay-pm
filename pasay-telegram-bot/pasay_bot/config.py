"""Bot configuration (pydantic-settings). Env vars are case-insensitive:
``PASSAY_TG_BOT_TOKEN`` maps to ``pasay_tg_bot_token``, etc.

NOTE: pydantic-settings in some venvs fails to bind env vars whose field
name starts with the ``pasay_`` prefix (silently ignoring them). To make the
bot deployable regardless, :func:`get_settings` reads ``.env`` / process env
with python-dotenv and passes values explicitly, rather than relying on
pydantic-settings' own env-variable name mapping.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_MINI_APP_URL = "https://pasay-mini-app.pages.dev"

# Issue #119 P0 ACCEPTANCE-ITEM-1: the legacy V1.1 native launchd bot ran as
# ``jhackuy`` on macOS where ``/opt/pasay-pm`` was the operator-owned deploy
# target (PRODUCTION_REVIEW.md / NATIVE_BOT_DESIGN.md). The Cloudflare
# Container production runtime instead runs as the unprivileged ``appuser``
# (uid 10000) on a slim Python image — see ``Dockerfile`` step 5/6/7:
# ``/opt`` is root:root and ``appuser`` has no write access there, while
# ``/app/uploads`` is the only directory chowned to ``appuser``. Without a
# Container-compatible default, the first webhook would raise
# ``PermissionError: [Errno 13] Permission denied: '/opt/pasay-pm'`` from
# :class:`pasay_bot.state.store.StateStore` and every bot.boot.send_message
# (including the canonical ``/start`` greeting + every persistent-keyboard
# action) would silently fail (webhook layer classifies PermissionError as
# TEMPORARY → HTTP 503 → Telegram redelivery; after the cross-attempt budget
# is exhausted Telegram marks the update ``failed`` and stops replaying).
# ``/tmp`` is sticky-bit world-writable on every POSIX image including the
# Cloudflare Container one, and the Container is the ``pasay-singleton``
# instance (``sleepAfter=15m``), so conversation/idempotency state survives
# all normal wake/sleep cycles. Operators still can override with ``STATE_DB``.
DEFAULT_BOT_STATE_DB = "/tmp/pasay-telegram-bot/state/bot_state.db"


class Settings(BaseSettings):
    pasay_tg_bot_token: str = ""
    pasay_api_base: str = "http://127.0.0.1:8000/api/v1"
    pasay_api_key: str = ""
    pasay_admin_api_key: str = ""
    hermes_api_base: str = "http://127.0.0.1:8642"
    hermes_api_key: str = ""
    state_db: str = DEFAULT_BOT_STATE_DB
    hook_token: str = ""
    callback_ttl_seconds: int = 900
    pasay_http_timeout_seconds: float = 30.0
    archive_chat_id: str = ""
    pasay_job_api_key: str = ""
    # Issue #119 Mini App — canonical production Pages origin. Environment
    # can override this, but production does not require an operator to wire
    # a non-secret URL manually after every deploy.
    pasay_mini_app_url: str = DEFAULT_MINI_APP_URL
    pasay_mini_app_owner_telegram_ids: str = ""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )


def _env() -> dict:
    data: dict = {}
    try:
        from dotenv import dotenv_values
        data.update({k: (v or "") for k, v in dotenv_values(".env").items()})
    except Exception:
        pass
    for key, val in os.environ.items():
        if key in {
            "PASSAY_TG_BOT_TOKEN", "PASSAY_API_BASE", "PASSAY_API_KEY",
            "PASSAY_ADMIN_API_KEY", "HERMES_API_BASE", "HERMES_API_KEY",
            "STATE_DB", "HOOK_TOKEN", "CALLBACK_TTL_SECONDS",
            "PASSAY_HTTP_TIMEOUT_SECONDS", "PASSAY_ARCHIVE_CHAT_ID",
            "PASSAY_JOB_API_KEY",
            "PASSAY_MINI_APP_URL", "PASSAY_MINI_APP_OWNER_TELEGRAM_IDS",
        }:
            data[key] = val
    # Issue #119 P0 Telegram-runtime defensive fallback: the Cloudflare
    # Worker historically only forwarded the raw Telegram token under the
    # upstream name ``TELEGRAM_BOT_TOKEN``; the bot reads it under
    # ``PASSAY_TG_BOT_TOKEN``. If the operator has not provisioned a
    # dedicated ``PASSAY_TG_BOT_TOKEN`` Worker secret yet, accept the
    # upstream token as a fallback so ``builder.token("")"`` does not
    # silently default to ``"0:UNSET"`` (every send_message would then hit
    # api.telegram.org/bot0:UNSET/sendMessage, Telegram returns
    # InvalidToken, the handler fails PERMANENTLY, the update is marked
    # ``failed`` and the Owner sees NO visible reply — the exact user-visible
    # failure mode from Issue #119). The Cloudflare Worker now also forwards
    # ``TELEGRAM_BOT_TOKEN`` as ``PASSAY_TG_BOT_TOKEN`` directly (see
    # ``cloudflare-worker/src/index.ts::PasayContainer.envVars``) so this
    # fallback is purely a second line of defence.
    if not data.get("PASSAY_TG_BOT_TOKEN") and os.environ.get("TELEGRAM_BOT_TOKEN"):
        data["PASSAY_TG_BOT_TOKEN"] = os.environ["TELEGRAM_BOT_TOKEN"]
    return data


@lru_cache
def get_settings() -> Settings:
    e = _env()
    return Settings(
        pasay_tg_bot_token=e.get("PASSAY_TG_BOT_TOKEN", ""),
        pasay_api_base=e.get("PASSAY_API_BASE", "http://127.0.0.1:8000/api/v1"),
        pasay_api_key=e.get("PASSAY_API_KEY", ""),
        pasay_admin_api_key=e.get("PASSAY_ADMIN_API_KEY", ""),
        hermes_api_base=e.get("HERMES_API_BASE", "http://127.0.0.1:8642"),
        hermes_api_key=e.get("HERMES_API_KEY", ""),
        state_db=(e.get("STATE_DB") or DEFAULT_BOT_STATE_DB).strip(),
        hook_token=e.get("HOOK_TOKEN", ""),
        callback_ttl_seconds=int(e.get("CALLBACK_TTL_SECONDS", "900") or "900"),
        pasay_http_timeout_seconds=float(e.get("PASSAY_HTTP_TIMEOUT_SECONDS", "30") or "30"),
        archive_chat_id=(e.get("PASSAY_ARCHIVE_CHAT_ID") or "").strip(),
        pasay_job_api_key=(e.get("PASSAY_JOB_API_KEY") or "").strip(),
        pasay_mini_app_url=(e.get("PASSAY_MINI_APP_URL") or DEFAULT_MINI_APP_URL).strip(),
        pasay_mini_app_owner_telegram_ids=(e.get("PASSAY_MINI_APP_OWNER_TELEGRAM_IDS") or "").strip(),
    )
