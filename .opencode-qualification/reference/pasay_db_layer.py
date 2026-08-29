"""PASAY reference implementation — SQLAlchemy base, mixins, session factory.

Issue #99 / PR #100 /oc continuation. Staged under
``.opencode-qualification/reference/`` pending Owner-side promotion to
``app/db/base.py`` and ``app/db/session.py``.

Hard invariants enforced by this module:
    * Declarative ``Base`` with explicit naming conventions so Alembic
      autogenerate produces stable constraint names across migrations.
    * ``AuditMixin`` adds ``created_at`` / ``updated_at`` (server-default
      ``now() AT TIME ZONE 'UTC'``) on every business table.
    * ``OrgScopedMixin`` enforces the Organization permission boundary at
      the schema level — every business row MUST carry ``org_id`` and
      queries MUST filter by it.
    * ``IdempotencyMixin`` enforces ``(org_id, endpoint, payload_hash,
      idempotency_key)`` UNIQUE — duplicate-key violations surface as
      HTTP 409 at the API layer.
    * Engine uses ``pool_pre_ping=True`` so Neon "wake up" reconnects are
      safe; ``pool_recycle=280`` keeps idle connections under Neon
      serverless idle timeout.
    * No floats anywhere; ``Numeric(14, 2)`` for money columns.

Reference promotion requires no behavioural change beyond file moves.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    MetaData,
    String,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    declared_attr,
    mapped_column,
    sessionmaker,
)

# Stable naming convention → Alembic migrations stay diff-friendly.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base.

    Every ORM model MUST inherit from this Base. The metadata uses the
    naming convention above so that Alembic autogenerate emits stable
    constraint names.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class AuditMixin:
    """Adds ``created_at`` / ``updated_at`` (UTC) to a business table.

    Server-side defaults via ``now() AT TIME ZONE 'UTC'`` so the column is
    populated even when the application forgets. Python-side ``default`` is
    a backup for tests using an in-memory SQLite engine.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now() AT TIME ZONE 'UTC'"),
        default=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now() AT TIME ZONE 'UTC'"),
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )


class OrgScopedMixin:
    """Adds ``org_id`` to a business table and indexes it.

    Every query against a business table MUST filter by ``org_id``. The
    application layer enforces this via ``require_org_scope``; the schema
    layer provides the index so the filter is fast.
    """

    org_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    @declared_attr.directive
    def __table_args__(cls) -> Any:  # noqa: N805
        return (
            Index(
                "ix_" + cls.__tablename__ + "_org_id",
                "org_id",
            ),
        )


class IdempotencyMixin:
    """Composite uniqueness for idempotency tables.

    The unique constraint covers (org_id, endpoint, idempotency_key,
    payload_hash) so the same opaque client key can be safely retried
    against the same logical operation while cross-tenant collisions are
    impossible.
    """

    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    @declared_attr.directive
    def __table_args__(cls) -> Any:  # noqa: N805
        return (
            UniqueConstraint(
                "org_id",
                "endpoint",
                "idempotency_key",
                "payload_hash",
                name="uq_idempotency_composite",
            ),
        )


# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

_DEFAULT_DB_URL = (
    "postgresql+psycopg2://pasay_pm:pasay_pm@localhost:5432/pasay_pm"
)


def _resolve_db_url() -> str:
    """Read DATABASE_URL from environment, falling back to local default."""
    return os.environ.get("DATABASE_URL", _DEFAULT_DB_URL)


def build_engine(
    url: Optional[str] = None,
    *,
    pool_pre_ping: bool = True,
    pool_recycle: int = 280,
    echo: bool = False,
) -> Engine:
    """Build a SQLAlchemy Engine tuned for Neon serverless + local dev.

    ``pool_recycle=280`` is intentionally under Neon's idle timeout so
    long-idle pools reconnect before the server forces them.
    """
    engine = create_engine(
        url or _resolve_db_url(),
        pool_pre_ping=pool_pre_ping,
        pool_recycle=pool_recycle,
        echo=echo,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_session_timezone(dbapi_conn: Any, _conn_record: Any) -> None:
        # Force session timezone to UTC so naive timestamps (which we forbid
        # but may slip through debugging SQL) are interpreted in UTC.
        try:
            cur = dbapi_conn.cursor()
            cur.execute("SET TIME ZONE 'UTC'")
            cur.close()
        except Exception:
            # SQLite (used in tests) doesn't support SET TIME ZONE — ignore.
            pass

    return engine


_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker[Session]] = None


def get_engine() -> Engine:
    """Process-wide cached engine. Created lazily on first call."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Process-wide cached sessionmaker."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
    return _SessionLocal


def reset_engine_cache() -> None:
    """Drop the cached engine / sessionmaker. Used by tests only."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a Session and closes it cleanly.

    Usage:
        @app.get("/x")
        def x(db: Session = Depends(get_db)):
            ...

    The session is committed ONLY by callers that explicitly call
    ``db.commit()``; the dependency never auto-commits.
    """
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()


__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "AuditMixin",
    "OrgScopedMixin",
    "IdempotencyMixin",
    "build_engine",
    "get_engine",
    "get_session_factory",
    "reset_engine_cache",
    "get_db",
]
