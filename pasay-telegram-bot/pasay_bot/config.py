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


class Settings(BaseSettings):
    pasay_tg_bot_token: str = ""
    pasay_api_base: str = "http://127.0.0.1:8000/api/v1"
    pasay_api_key: str = ""
    pasay_admin_api_key: str = ""
    hermes_api_base: str = "http://127.0.0.1:8642"
    hermes_api_key: str = ""
    state_db: str = "/opt/pasay-pm/pasay-telegram-bot/state/bot_state.db"
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
        state_db=e.get("STATE_DB", "/opt/pasay-pm/pasay-telegram-bot/state/bot_state.db"),
        hook_token=e.get("HOOK_TOKEN", ""),
        callback_ttl_seconds=int(e.get("CALLBACK_TTL_SECONDS", "900") or "900"),
        pasay_http_timeout_seconds=float(e.get("PASSAY_HTTP_TIMEOUT_SECONDS", "30") or "30"),
        archive_chat_id=(e.get("PASSAY_ARCHIVE_CHAT_ID") or "").strip(),
        pasay_job_api_key=(e.get("PASSAY_JOB_API_KEY") or "").strip(),
        pasay_mini_app_url=(e.get("PASSAY_MINI_APP_URL") or DEFAULT_MINI_APP_URL).strip(),
        pasay_mini_app_owner_telegram_ids=(e.get("PASSAY_MINI_APP_OWNER_TELEGRAM_IDS") or "").strip(),
    )
