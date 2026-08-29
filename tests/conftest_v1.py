"""Pytest fixtures for the V1 rewrite test suite.

These fixtures are imported by future V1 slice tests. They set DATABASE_URL
before any app/db import so the engine is created cleanly.
"""
from __future__ import annotations

import os

# Set DATABASE_URL before importing app modules that read it at import time.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:?check_same_thread=False")

import pytest

from app.db.session import (
    bind_engine,
    get_engine,
    get_session_factory,
    reset_engine_cache,
)


_TEST_DB_URL = "sqlite:///:memory:?check_same_thread=False"


@pytest.fixture
def db_engine():
    """A fresh SQLite in-memory engine."""
    reset_engine_cache()
    engine = bind_engine(_TEST_DB_URL)
    yield engine
    reset_engine_cache()


@pytest.fixture
def db_session(db_engine):
    """A SQLAlchemy session bound to the test engine."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
