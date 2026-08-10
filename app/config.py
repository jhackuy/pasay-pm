from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg2://pasay_pm:pasay_pm@localhost:5432/pasay_pm"
    upload_dir: str = "uploads"
    # V1.2: bot token used by the notifier worker for sendMessage. Leave empty
    # to run the scheduler without sending notifications.
    telegram_bot_token: str = ""


settings = Settings()
