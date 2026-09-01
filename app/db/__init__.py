"""PASAY database layer.

Constitutional invariants (AGENTS.md §4):
- Operation is Truth, Task is Projection.
- Money: NUMERIC(14, 2) / Decimal — never float.
- Time: timestamptz / UTC-aware datetime — never naive.
- Permission boundary: Organization + Membership, fail-closed.
"""
from app.db.base import (
    AuditMixin,
    Base,
    IdempotencyMixin,
    OrgScopedMixin,
)
from app.db.session import (
    bind_engine,
    get_db,
    get_engine,
    reset_engine_cache,
    session_scope,
)

__all__ = [
    "AuditMixin",
    "Base",
    "IdempotencyMixin",
    "OrgScopedMixin",
    "bind_engine",
    "get_db",
    "get_engine",
    "reset_engine_cache",
    "session_scope",
]
