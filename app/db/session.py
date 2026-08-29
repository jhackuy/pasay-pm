"""SQLAlchemy engine and session factory.

AGENTS.md §4 (long-lived containers): if DATABASE_URL rotates (secret
rotation in a Cloudflare Container), the engine MUST rebind. We provide
two layers of defense:
  - `get_engine()` rebinds automatically when DATABASE_URL changes since
    the previous bind (detected via `_bound_url`).
  - `bind_engine(url)` forces a rebind for explicit use at container
    startup, after secret rotation, or in tests.
  - `reset_engine_cache()` clears state for the next lazy rebind.

The session factory is created lazily alongside the engine and is
recreated on every bind. There is no module-global cached engine that
survives a DATABASE_URL change without rebinding.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None
_lock = threading.Lock()
_bound_url: Optional[str] = None


def get_engine() -> Engine:
    """Return the bound engine, rebinding if DATABASE_URL has changed."""
    global _engine, _session_factory, _bound_url
    url = os.environ.get("DATABASE_URL")
    if url is None:
        raise RuntimeError("DATABASE_URL is not set")
    with _lock:
        if _engine is None or _bound_url != url:
            if _engine is not None:
                _engine.dispose()
            _engine = create_engine(url, pool_pre_ping=True, future=True)
            _session_factory = sessionmaker(
                bind=_engine,
                autoflush=False,
                autocommit=False,
                future=True,
            )
            _bound_url = url
        return _engine


def bind_engine(url: str) -> Engine:
    """Force-bind a new engine to the given DATABASE_URL.

    Disposes the old engine if present. Use on container startup, after
    secret rotation, or in tests.
    """
    global _engine, _session_factory, _bound_url
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = create_engine(url, pool_pre_ping=True, future=True)
        _session_factory = sessionmaker(
            bind=_engine,
            autoflush=False,
            autocommit=False,
            future=True,
        )
        _bound_url = url
        return _engine


def reset_engine_cache() -> None:
    """Dispose and clear the cached engine and session factory."""
    global _engine, _session_factory, _bound_url
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None
        _bound_url = None


def get_session_factory() -> sessionmaker:
    """Return the sessionmaker, creating it (and the engine) lazily."""
    global _session_factory
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a SQLAlchemy session.

    Commits on success, rolls back on exception, always closes.
    """
    factory = get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for non-request DB access (jobs, scripts)."""
    factory = get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
