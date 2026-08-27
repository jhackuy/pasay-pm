"""Minimal alembic env.py used by fixtures.

The static graph gate does not execute env.py — only static-checks and the
authority check call `ScriptDirectory.from_config` which only needs the
script_location to find `versions/`. Keep this minimal so Alembic itself
can construct the ScriptDirectory cleanly.

No DB engine is initialised here because the fixtures are exercised by the
static + ScriptDirectory-only path. Real `alembic upgrade head` against a
fresh DB uses the project's actual env.py, not this one.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
