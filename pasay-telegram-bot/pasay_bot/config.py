"""Bot configuration (pydantic-settings). Env vars are case-insensitive:
``PASSAY_TG_BOT_TOKEN`` maps to ``pasay_tg_bot_token``, etc.

NOTE: pydantic-settings in some venvs fails to bind env vars whose field
name starts with the ``pasay_`` prefix (silently ignoring them). To make the
bot deployable regardless, :func:`get_settings` reads ``.env`` / process env
with python-dotenv and passes values explicitly, rather than relying on
pydantic-settings' own env-variable name mapping.

RETURN1-FIX B: default state_db moved from /opt/pasay-pm (unwritable in
Docker as non-root) to /app/state/bot_state.db (under WORKDIR /app).
RETURN1-FIX C: also accepts TELEGRAM_BOT_TOKEN directly as an overlay source
so the bot works inside Cloudflare Container without duplicated secrets.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    pasay_tg_bot_token: str = ""
    pasay_api_base: str = "http://127.0.0.1:8000/api/v1"
    pasay_api_key: str = ""
    pasay_admin_api_key: str = ""
    hermes_api_base: str = "http://127.0.0.1:8642"
    hermes_api_key: str = ""
    state_db: str = "/app/state/bot_state.db"
    hook_token: str = ""
    callback_ttl_seconds: int = 900
    pasay_http_timeout_seconds: float = 30.0
    archive_chat_id: str = ""
    pasay_job_api_key: str = ""
    pasay_runtime_mode: str = ""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )


def _env() -> dict:
    """Best-effort .env + process-env overlay (python-dotenv if available).

    RETURN1: also accepts TELEGRAM_BOT_TOKEN directly (same value as
    PASSAY_TG_BOT_TOKEN and PASSAY_API_KEY since STEP 7 hashes the raw
    TELEGRAM_BOT_TOKEN sha256 into ApiCredential(native-bot/telegram_bot)).
    """
    data: dict = {}
    try:
        from dotenv import dotenv_values
        data.update({k: (v or "") for k, v in dotenv_values(".env").items()})
    except Exception:
        pass
    # process environment overrides .env
    direct_aliases = {
        "TELEGRAM_BOT_TOKEN": ("PASSAY_TG_BOT_TOKEN", "PASSAY_API_KEY"),
        "TELEGRAM_WEBHOOK_SECRET": ("HOOK_TOKEN",),
    }
    for key, val in os.environ.items():
        if key in {
            "PASSAY_TG_BOT_TOKEN", "PASSAY_API_BASE", "PASSAY_API_KEY",
            "PASSAY_ADMIN_API_KEY", "HERMES_API_BASE", "HERMES_API_KEY",
            "STATE_DB", "HOOK_TOKEN", "CALLBACK_TTL_SECONDS",
            "PASSAY_HTTP_TIMEOUT_SECONDS", "PASSAY_ARCHIVE_CHAT_ID",
            "PASSAY_JOB_API_KEY", "PASAY_RUNTIME_MODE",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
        }:
            data[key] = val
    # Apply direct aliases (process env wins if already explicit).
    for src, dsts in direct_aliases.items():
        sv = (os.environ.get(src) or "").strip()
        if not sv:
            continue
        for dst in dsts:
            if dst not in data or not (data.get(dst) or "").strip():
                data[dst] = sv
    if "PASAY_RUNTIME_MODE" in data and "PASSAY_RUNTIME_MODE" not in data:
        data["PASSAY_RUNTIME_MODE"] = data["PASAY_RUNTIME_MODE"]
    return data


@lru_cache
def get_settings() -> Settings:
    e = _env()
    tg_override = (e.get("TELEGRAM_BOT_TOKEN") or "").strip()
    return Settings(
        pasay_tg_bot_token=(
            e.get("PASSAY_TG_BOT_TOKEN") or tg_override or ""
        ),
        pasay_api_base=e.get("PASSAY_API_BASE", "http://127.0.0.1:8000/api/v1"),
        pasay_api_key=(
            e.get("PASSAY_API_KEY") or tg_override or ""
        ),
        pasay_admin_api_key=e.get("PASSAY_ADMIN_API_KEY", ""),
        hermes_api_base=e.get("HERMES_API_BASE", "http://127.0.0.1:8642"),
        hermes_api_key=e.get("HERMES_API_KEY", ""),
        state_db=e.get("STATE_DB", "/app/state/bot_state.db"),
        hook_token=e.get("HOOK_TOKEN", ""),
        callback_ttl_seconds=int(e.get("CALLBACK_TTL_SECONDS", "900") or "900"),
        pasay_http_timeout_seconds=float(e.get("PASSAY_HTTP_TIMEOUT_SECONDS", "30") or "30"),
        archive_chat_id=(e.get("PASSAY_ARCHIVE_CHAT_ID") or e.get("ARCHIVE_CHAT_ID") or "").strip(),
        pasay_job_api_key=(e.get("PASSAY_JOB_API_KEY") or "").strip(),
        pasay_runtime_mode=(e.get("PASAY_RUNTIME_MODE") or e.get("PASSAY_RUNTIME_MODE") or "").strip(),
    )
