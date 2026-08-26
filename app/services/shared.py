"""Shared service helpers used across both routers and service modules to
avoid reverse (service -> router) imports.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.lease import Lease, LeaseStatus
from app.models.property import Unit, UnitStatus


def sync_unit_status(db: Session, unit: Unit) -> tuple[UnitStatus, UnitStatus]:
    """Recompute Unit.status based on whether any active (non-deleted) lease
    currently occupies this unit.

    Previously lived in app/api/routers/leases.py as module-level
    ``_sync_unit_status``; migrated here so service-layer code (e.g.
    move_out_workflow) can call it without a service -> router reverse import.

    Caller is responsible for locking the Unit row FOR UPDATE and for deciding
    whether to write an Audit row — this helper does not write Audit.

    Returns (old_status_enum, new_status_enum) so caller can compare by value.
    """
    old_status = unit.status
    active = (
        db.query(Lease)
        .filter(Lease.unit_id == unit.id, Lease.status == LeaseStatus.active, Lease.deleted_at.is_(None))
        .first()
    )
    if active is not None:
        unit.status = UnitStatus.occupied
    elif unit.status == UnitStatus.occupied:
        unit.status = UnitStatus.vacant
    return old_status, unit.status
