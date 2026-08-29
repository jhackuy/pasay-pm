"""V1 clean rewrite namespace (Issue #99, PR #100).

Layers:
- models: SQLAlchemy ORM (Org/Membership/Property/Unit/Tenant/Lease).
- schemas: Pydantic v2 DTOs.
- services: application/domain logic with transaction boundaries.
- api: thin FastAPI routers.

AGENTS.md invariants enforced in services and DB constraints:
Decimal money, UTC time, Org/Membership fail-closed, idempotency.
"""
__version__ = "1.0.0"