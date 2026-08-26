"""PASAY-M004 DB-invariant direct-SQL probes (Owner PASAY-TASK-012 #2).

Each probe uses an explicit SAVEPOINT so a DB-denied IntegrityError does not
pollute the outer transaction (the test script itself must still be usable).
The 4 probes are:
  a) same unit_id + same tenant_id successor INSERT  → COMMIT OK
  b) cross unit_id same tenant_id  → DB ERROR  (savepoint rollback)
  c) same unit_id cross tenant_id  → DB ERROR  (savepoint rollback)
  d) non-existent successor id    → DB ERROR  (savepoint rollback)

These tests are written so they can run BOTH:
  * inside a disposable postgres DB seeded via alembic upgrade head
    (pytest conftest.py style when TestClient/SessionLocal exists)
  * OR standalone via `alembic + raw connection + psycopg2 conn autocommit off`
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, InternalError

from app.models.lease import Lease, LeaseStatus
from app.models.property import Unit, Property
from app.models.tenant import Tenant
from app.models.membership import Organization


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_org_unit_tenant(db):
    """Create Organization + Property + 2 Units + 2 Tenants.

    Returns (org, prop, u1, u2, t1, t2).
    """
    org = Organization(name="db-inv-org")
    db.add(org)
    db.flush()
    prop = Property(
        organization_id=org.id,
        name="p-inv",
        address="addr-inv",
        city="c-inv",
        total_units=2,
    )
    db.add(prop)
    db.flush()
    from decimal import Decimal
    u1 = Unit(
        property_id=prop.id,
        unit_number="U-INV-1",
        monthly_rent=Decimal("1000"),
    )
    u2 = Unit(
        property_id=prop.id,
        unit_number="U-INV-2",
        monthly_rent=Decimal("2000"),
    )
    t1 = Tenant(organization_id=org.id, full_name="Tenant Inv 1")
    t2 = Tenant(organization_id=org.id, full_name="Tenant Inv 2")
    db.add_all([u1, u2, t1, t2])
    db.flush()
    return org, prop, u1, u2, t1, t2


def _lease(db, *, unit, tenant, start, end, status=LeaseStatus.active):
    from decimal import Decimal
    l = Lease(
        unit_id=unit.id,
        tenant_id=tenant.id,
        start_date=start,
        end_date=end,
        monthly_rent=Decimal("1000"),
        deposit=Decimal("500"),
        status=status,
    )
    db.add(l)
    db.flush()
    return l


# ---------------------------------------------------------------------------
# Fixture-aware pytest tests. Running disposable PG is handled by conftest.
# ---------------------------------------------------------------------------


def test_inv1_same_unit_same_tenant_successor_success(db_session):
    """Probe (a): successor with same (unit, tenant) → must INSERT + COMMIT OK.

    Satisfies pre-existing ck_leases_superseded_pair (pred.status=expired AND
    superseded_at not-null required when superseded_by_lease_id != NULL).
    """
    from datetime import date, datetime, timedelta, timezone
    _org, _prop, u1, _u2, t1, _t2 = _seed_org_unit_tenant(db_session)
    start = date(2025, 1, 1)
    mid   = date(2025, 12, 31)
    end2  = mid + timedelta(days=365)
    pred = _lease(db_session, unit=u1, tenant=t1, start=start, end=mid)
    succ = _lease(db_session, unit=u1, tenant=t1, start=mid + timedelta(days=1), end=end2)
    try:
        pred.status = "expired"
        pred.superseded_at = datetime.now(timezone.utc)
        pred.superseded_by_lease_id = succ.id
        db_session.flush()
        db_session.commit()
    except Exception as exc:
        db_session.rollback()
        pytest.fail(
            "SAME unit SAME tenant successor INSERT must not be rejected by DB "
            f"(same-party composite FK + ck_leases_superseded_pair should allow it). "
            f"Exception: {exc!r}"
        )
    db_session.expire_all()
    reloaded = db_session.get(Lease, pred.id)
    assert reloaded.superseded_by_lease_id == succ.id, (
        "same-party successor link not persisted; DB composite FK / unique "
        "constraint broken?"
    )
    assert reloaded.status.value == "expired"
    assert reloaded.superseded_at is not None


def test_inv2_cross_unit_same_tenant_successor_db_rejected(db_session):
    """Probe (b): cross unit / same tenant successor → DB REJECTED."""
    from datetime import date, datetime, timedelta, timezone
    _org, _prop, u1, u2, t1, _t2 = _seed_org_unit_tenant(db_session)
    start = date(2025, 1, 1)
    mid   = date(2025, 12, 31)
    pred = _lease(db_session, unit=u1, tenant=t1, start=start, end=mid)
    succ = _lease(db_session, unit=u2, tenant=t1, start=mid + timedelta(days=1), end=mid + timedelta(days=365))
    db_session.flush()
    pred_id = pred.id
    saved_succ_id = succ.id
    raised = False
    sp = db_session.begin_nested()
    try:
        pred.status = "expired"
        pred.superseded_at = datetime.now(timezone.utc)
        pred.superseded_by_lease_id = saved_succ_id
        db_session.flush()
        sp.commit()
    except (IntegrityError, InternalError):
        raised = True
        sp.rollback()
    assert raised is True, (
        "cross-unit same-tenant successor MUST be rejected by PostgreSQL "
        "composite FK fk_leases_superseded_same_party. Got clean flush = DB "
        "invariant not enforced."
    )
    # Only the nested SAVEPOINT was rolled back by sp.rollback() above — the
    # outer transaction still holds the seeded pred/succ rows. Expire the ORM
    # identity map so re-reading pred_id gives the true DB state (which was
    # never modified thanks to the savepoint isolation).
    db_session.expire_all()
    remaining = db_session.query(Lease.superseded_by_lease_id).filter(
        Lease.id == pred_id
    ).one()
    assert remaining[0] is None, (
        "after cross-unit probe rollback, predecessor superseded_by_lease_id "
        "must still be NULL (outer tx unpolluted via nested savepoint)."
    )


def test_inv3_same_unit_cross_tenant_successor_db_rejected(db_session):
    """Probe (c): same unit / cross tenant successor → DB REJECTED."""
    from datetime import date, datetime, timedelta, timezone
    _org, _prop, u1, _u2, t1, t2 = _seed_org_unit_tenant(db_session)
    start = date(2025, 1, 1)
    mid   = date(2025, 12, 31)
    pred = _lease(db_session, unit=u1, tenant=t1, start=start, end=mid)
    succ = _lease(db_session, unit=u1, tenant=t2, start=mid + timedelta(days=1), end=mid + timedelta(days=365))
    db_session.flush()
    pred_id = pred.id
    saved_succ_id = succ.id
    raised = False
    sp = db_session.begin_nested()
    try:
        pred.status = "expired"
        pred.superseded_at = datetime.now(timezone.utc)
        pred.superseded_by_lease_id = saved_succ_id
        db_session.flush()
        sp.commit()
    except (IntegrityError, InternalError):
        raised = True
        sp.rollback()
    assert raised is True, (
        "same-unit cross-tenant successor MUST be rejected by PostgreSQL "
        "composite FK fk_leases_superseded_same_party."
    )
    db_session.expire_all()
    remaining = db_session.query(Lease.superseded_by_lease_id).filter(
        Lease.id == pred_id
    ).one()
    assert remaining[0] is None


def test_inv4_nonexistent_successor_id_db_rejected(db_session):
    """Probe (d): non-existent successor id → DB REJECTED."""
    from datetime import date, datetime, timezone
    _org, _prop, u1, _u2, t1, _t2 = _seed_org_unit_tenant(db_session)
    pred = _lease(db_session, unit=u1, tenant=t1, start=date(2025, 1, 1), end=date(2025, 12, 31))
    db_session.flush()
    pred_id = pred.id
    nonexistent_id = 9_999_999
    raised = False
    sp = db_session.begin_nested()
    try:
        pred.status = "expired"
        pred.superseded_at = datetime.now(timezone.utc)
        pred.superseded_by_lease_id = nonexistent_id
        db_session.flush()
        sp.commit()
    except (IntegrityError, InternalError):
        raised = True
        sp.rollback()
    assert raised is True, (
        "non-existent successor_id MUST be rejected by PostgreSQL FK "
        "invariant (single-col fk_leases_superseded_by + composite "
        "fk_leases_superseded_same_party)."
    )
    db_session.expire_all()
    remaining = db_session.query(Lease.superseded_by_lease_id).filter(
        Lease.id == pred_id
    ).one()
    assert remaining[0] is None
