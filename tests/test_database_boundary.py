from types import SimpleNamespace

from app.infrastructure.postgres import (
    build_postgres_runtime_boundary,
    normalize_sqlalchemy_postgres_url,
)


def test_normalize_sqlalchemy_postgres_url_supports_generic_postgresql_scheme():
    assert (
        normalize_sqlalchemy_postgres_url("postgresql://user:pw@localhost:5432/pasay_pm")
        == "postgresql+psycopg2://user:pw@localhost:5432/pasay_pm"
    )


def test_build_postgres_runtime_boundary_uses_unpooled_url_for_migrations():
    settings = SimpleNamespace(
        database_url="postgresql://user:pw@ep-cool-pooler.us-east-1.aws.neon.tech/neondb",
        database_url_unpooled="postgresql://user:pw@ep-cool.us-east-1.aws.neon.tech/neondb",
    )

    boundary = build_postgres_runtime_boundary(settings)

    assert boundary.provider == "neon"
    assert boundary.application_connection_mode == "pooled"
    assert boundary.migration_connection_mode == "direct"
    assert boundary.application_url.startswith("postgresql+psycopg2://")
    assert boundary.migration_url.startswith("postgresql+psycopg2://")
