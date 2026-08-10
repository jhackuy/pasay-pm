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

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )


def _env() -> dict:
    """Best-effort .env + process-env overlay (python-dotenv if available)."""
    data: dict = {}
    try:
        from dotenv import dotenv_values
        data.update({k: (v or "") for k, v in dotenv_values(".env").items()})
    except Exception:
        pass
    # process environment overrides .env
    for key, val in os.environ.items():
        if key in {
            "PASSAY_TG_BOT_TOKEN", "PASSAY_API_BASE", "PASSAY_API_KEY",
            "PASSAY_ADMIN_API_KEY", "HERMES_API_BASE", "HERMES_API_KEY",
            "STATE_DB", "HOOK_TOKEN", "CALLBACK_TTL_SECONDS",
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
    )
