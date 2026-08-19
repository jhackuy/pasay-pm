from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg2://pasay_pm:pasay_pm@localhost:5432/pasay_pm"
    database_url_unpooled: str | None = None
    upload_dir: str = "uploads"
    # V1.2: bot token used by the notifier worker for sendMessage. Leave empty
    # to run the scheduler without sending notifications.
    telegram_bot_token: str = ""
    # V1.2.2 Phase C2 kill-switch: authorizes the human-confirmed Copilot
    # executor (create_followup_task / assign_task / snooze_task). Default
    # FALSE (fail closed); set COPILOT_EXECUTION_ENABLED=true in the running
    # deployment ONLY after the C2 suite is green. Mirrored in
    # app/services/operations/copilot.py::COPILOT_EXECUTION_ENABLED (one source
    # of truth: env wins, module default matches .env.example).
    copilot_execution_enabled: bool = False


settings = Settings()
