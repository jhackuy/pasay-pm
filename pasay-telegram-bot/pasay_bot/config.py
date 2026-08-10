"""Bot configuration (pydantic-settings). Env vars are case-insensitive:
``PASSAY_TG_BOT_TOKEN`` maps to ``pasay_tg_bot_token``, etc."""
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
