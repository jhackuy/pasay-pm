from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker


def normalize_sqlalchemy_postgres_url(database_url: str) -> str:
    """Keep legacy local URLs working while accepting generic Postgres URLs."""
    if database_url.startswith("postgresql+"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg2://", 1)
    return database_url


def detect_connection_mode(database_url: str) -> str:
    return "pooled" if "-pooler." in database_url else "direct"


def detect_database_provider(database_url: str) -> str:
    lowered = database_url.lower()
    return "neon" if "neon.tech" in lowered else "postgres"


@dataclass(frozen=True)
class PostgresRuntimeBoundary:
    application_url: str
    migration_url: str
    provider: str
    application_connection_mode: str
    migration_connection_mode: str

    @property
    def config_available(self) -> bool:
        return bool(self.application_url)


def build_postgres_runtime_boundary(settings) -> PostgresRuntimeBoundary:
    application_url = normalize_sqlalchemy_postgres_url(settings.database_url)
    migration_url = normalize_sqlalchemy_postgres_url(
        settings.database_url_unpooled or settings.database_url
    )
    return PostgresRuntimeBoundary(
        application_url=application_url,
        migration_url=migration_url,
        provider=detect_database_provider(application_url),
        application_connection_mode=detect_connection_mode(application_url),
        migration_connection_mode=detect_connection_mode(migration_url),
    )


def create_app_engine(boundary: PostgresRuntimeBoundary) -> Engine:
    return create_engine(boundary.application_url, pool_pre_ping=True)


def create_session_local(boundary: PostgresRuntimeBoundary) -> sessionmaker:
    engine = create_app_engine(boundary)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def redact_database_url(database_url: str) -> str:
    return make_url(database_url).render_as_string(hide_password=True)
