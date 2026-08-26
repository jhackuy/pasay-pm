from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_x_db_url = context.get_x_argument(as_dictionary=True).get("db_url")
if _x_db_url:
    config.set_main_option("sqlalchemy.url", _x_db_url)
else:
    config.set_main_option("sqlalchemy.url", settings.database_url)

_isolated_test_metadata = config.attributes.get("pasay.test.target_metadata", None)
if _isolated_test_metadata is not None:
    target_metadata = _isolated_test_metadata
else:
    target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
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
