"""RET3 Subagent A: targeted counter-example tests for backend-final-closeout issues.

Each test is 10-25 lines, one per issue, proving behavior is correct after fix.
No commit / no push — pure file edits + these pytest counter-examples.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

from sqlalchemy.orm import Session

from app.models.membership import (
    Membership,
    MembershipState,
    OrganizationRole,
    Organization,
)
from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Property, Unit
from app.models.lease import Lease, LeaseStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.models.financial import Expense, ExpenseStatus
from app.models.commission import CommissionSettlement, CommissionSettlementStatus
from app.models.deposit_settlement import DepositSettlement, DepositSettlementStatus
from app.models.move_out import MoveOutInspection, MoveOutInspectionStatus
from app.models.repair import RepairOperation
from tests.conftest import make_user, ensure_default_org, _headers


def _make_task(**overrides) -> OperationalTask:
    """Build a minimal valid OperationalTask row (all NOT NULL cols set)."""
    now = datetime.now(timezone.utc)
    base = dict(
        task_type=OperationalTaskType.FOLLOWUP,
        title="default-task",
        source_type="manual",
        priority=OperationalTaskPriority.medium,
        status=OperationalTaskStatus.PENDING,
        due_at=now,
        details={},
    )
    base.update(overrides)
    return OperationalTask(**base)


# ---------------------------------------------------------------------------
# Issue 2: tasks.py — no active membership -> HTTP 403 (fail-closed)
# ---------------------------------------------------------------------------


def test_issue2_list_tasks_no_membership_returns_403(client, db_session: Session):
    user, key = make_user(db_session, "loner", UserRole.admin)
    resp = client.get("/api/v1/tasks", headers=_headers(key))
    assert resp.status_code == 403, f"expected 403 got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "No active organization membership" in body.get("detail", "")


def test_issue2_get_task_no_membership_returns_403(client, db_session: Session):
    user, key = make_user(db_session, "loner2", UserRole.admin)
    resp = client.get("/api/v1/tasks/999", headers=_headers(key))
    assert resp.status_code == 403, f"expected 403 got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "No active organization membership" in body.get("detail", "")


# ---------------------------------------------------------------------------
# Issue 5: build_quick_expense — both None args -> zeroed result (fail-closed)
# ---------------------------------------------------------------------------


def test_issue5_build_quick_expense_no_args_returns_zeroed(db_session: Session):
    from app.services.operations.quick import build_quick_expense
    result = build_quick_expense(db_session)
    assert result["month_total"] == Decimal("0.00")
    assert result["pending_approval_count"] == 0
    assert result["pending_approval_amount"] == Decimal("0.00")
    assert result["unresolved_expense_tasks"] == []
    assert result["records"] == []
    assert result["payable"] == []
    assert result["paid_records"] == []


# ---------------------------------------------------------------------------
# Issue 6: OperationalTaskPriority.medium used (not .normal) in expense path
# ---------------------------------------------------------------------------


def test_issue6_priority_enum_has_no_normal_only_medium():
    valid_values = {p.value for p in OperationalTaskPriority}
    assert "normal" not in valid_values, ".normal must not exist per OperationalTaskPriority contract"
    assert "medium" in valid_values
    assert {*valid_values} == {"low", "medium", "high", "critical"}


def test_issue6_expense_repair_priority_truth_by_evidence_fields(db_session: Session):
    """Issue 6 Major (test truth via real details/evidence predicate path).

    The CodeRabbit finding correctly identified a false-pass: the previous
    version set ``fake_repair.attachment_ids`` but the real predicate
    :func:`_evidence_present_for_repair_close` inspects
    ``RepairOperation.details["completion_evidence_ids"]``. We verify both
    branches via the same attribute paths the production predicate reads.
    """
    from app.api.routers.expense import _evidence_present_for_repair_close

    # Branch A: no completion_evidence_ids written → predicate False → medium
    fake_repair_a = MagicMock(spec=RepairOperation)
    fake_repair_a.details = {}
    assert _evidence_present_for_repair_close(fake_repair_a) is False
    chosen_a = (
        OperationalTaskPriority.high
        if _evidence_present_for_repair_close(fake_repair_a)
        else OperationalTaskPriority.medium
    )
    assert chosen_a == OperationalTaskPriority.medium

    # Branch B: non-empty completion_evidence_ids in details → predicate True → high
    fake_repair_b = MagicMock(spec=RepairOperation)
    fake_repair_b.details = {"completion_evidence_ids": [101, 102, 103]}
    assert _evidence_present_for_repair_close(fake_repair_b) is True
    chosen_b = (
        OperationalTaskPriority.high
        if _evidence_present_for_repair_close(fake_repair_b)
        else OperationalTaskPriority.medium
    )
    assert chosen_b == OperationalTaskPriority.high


# ---------------------------------------------------------------------------
# Issue 7: multi-org user -> HTTP 409 (reports.py resolve_org_membership)
# ---------------------------------------------------------------------------


def test_issue7_reports_multi_membership_returns_409(client, db_session: Session):
    user, key = make_user(db_session, "multiown", UserRole.admin)
    org1 = Organization(name="Org Alpha")
    org2 = Organization(name="Org Beta")
    db_session.add_all([org1, org2])
    db_session.flush()
    db_session.add_all([
        Membership(user_id=user.id, organization_id=org1.id,
                   role=OrganizationRole.OWNER, state=MembershipState.ACTIVE),
        Membership(user_id=user.id, organization_id=org2.id,
                   role=OrganizationRole.OWNER, state=MembershipState.ACTIVE),
    ])
    db_session.commit()
    resp = client.get("/api/v1/reports/tasks", headers=_headers(key))
    assert resp.status_code == 409, f"expected 409 got {resp.status_code}: {resp.text}"
    assert "Organization context required" in resp.json().get("detail", "")


# ---------------------------------------------------------------------------
# Issue 8: operations.py uses canonical org_property_ids/org_lease_ids imports
# ---------------------------------------------------------------------------


def test_issue8_operations_uses_canonical_scope_imports():
    import importlib
    mod = importlib.import_module("app.api.routers.operations")
    assert getattr(mod, "_org_property_ids", None) is not None
    assert getattr(mod, "_org_lease_ids", None) is not None
    assert getattr(mod, "_org_tenant_ids", None) is not None
    from app.services.organization_scope import (
        org_property_ids, org_lease_ids, org_tenant_ids,
    )
    assert callable(org_property_ids) and callable(org_lease_ids) and callable(org_tenant_ids)


# ---------------------------------------------------------------------------
# Issue 11: exceptions.py imports timezone (timezone.utc used without crash)
# ---------------------------------------------------------------------------


def test_issue11_exceptions_has_timezone_import():
    from app.services.operations import exceptions as exc_mod
    assert getattr(exc_mod, "timezone", None) is timezone
    now = datetime.now(timezone.utc)
    assert now.tzinfo is not None


# ---------------------------------------------------------------------------
# Issue 13: quick.py build_quick_tasks uses Any, not SQLAlchemyFilterType
# ---------------------------------------------------------------------------


def test_issue13_quick_no_sqlalchemyfiltertype_signature(db_session: Session):
    import inspect
    from app.services.operations.quick import build_quick_tasks
    sig = inspect.signature(build_quick_tasks)
    task_scope_ann = sig.parameters["task_scope"].annotation
    assert "SQLAlchemyFilterType" not in str(task_scope_ann), (
        f"bad annotation left in signature: {task_scope_ann!r}"
    )
    assert "Any" in str(task_scope_ann)


# ---------------------------------------------------------------------------
# Issue 4: quick.py _derive_org_scope_sets does not swallow db.flush
# ---------------------------------------------------------------------------


def test_issue4_flush_not_swallowed(db_session: Session):
    from app.services.operations.quick import _derive_org_scope_sets
    ensure_default_org(db_session)
    org = db_session.query(Organization).first()
    user, _key = make_user(db_session, "member4", UserRole.admin)
    db_session.add(Membership(
        user_id=user.id, organization_id=org.id,
        role=OrganizationRole.OWNER, state=MembershipState.ACTIVE,
    ))
    db_session.commit()
    with patch.object(db_session, "flush", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            _derive_org_scope_sets(db_session, user_id=user.id)


# ---------------------------------------------------------------------------
# Issue 10: copilot _pending_expenses scoped by org_id (empty list when no match)
# ---------------------------------------------------------------------------


def test_issue10_copilot_pending_expenses_fail_closed_org_scope(db_session: Session):
    from app.services.operations.copilot import _pending_expenses
    ensure_default_org(db_session)
    org = db_session.query(Organization).first()
    rows = _pending_expenses(db_session, None, org_id=org.id + 99999)
    assert rows == [], "unknown org_id -> empty list fail-closed"


def test_issue10_copilot_pending_settlements_fail_closed_org_scope(db_session: Session):
    from app.services.operations.copilot import _pending_settlements
    user = MagicMock(spec=User)
    user.role = UserRole.admin
    ensure_default_org(db_session)
    org = db_session.query(Organization).first()
    rows = _pending_settlements(db_session, user, None, org_id=org.id + 99999)
    assert rows == [], "unknown org_id -> empty list fail-closed"


# ---------------------------------------------------------------------------
# Issue 1: reports.py tasks_report includes tasks linked directly via org_id in details
# ---------------------------------------------------------------------------


def test_issue1_reports_task_includes_org_bound_direct_link(client, db_session: Session):
    user, key = make_user(db_session, "own1", UserRole.admin)
    ensure_default_org(db_session)
    org = db_session.query(Organization).first()
    m = Membership(
        user_id=user.id, organization_id=org.id,
        role=OrganizationRole.OWNER, state=MembershipState.ACTIVE,
    )
    db_session.add(m)
    db_session.flush()
    task = _make_task(
        title="unbound direct org link",
        details={"organization_id": org.id},
    )
    db_session.add(task)
    db_session.commit()
    resp = client.get("/api/v1/reports/tasks", headers=_headers(key))
    assert resp.status_code == 200, f"unexpected {resp.status_code}: {resp.text}"
    titles = {t.get("title") for t in resp.json()}
    assert "unbound direct org link" in titles, (
        "direct details.organization_id link not included (cross-org leak fix broken)"
    )


# ---------------------------------------------------------------------------
# Issue 12: generation.py scans are org-scoped (no rows for unknown org_id)
# ---------------------------------------------------------------------------


def test_issue12_generation_scan_fail_closed_unknown_org(db_session: Session):
    from app.services.operations.generation import generate_business_tasks
    created, notifications = generate_business_tasks(
        db_session,
        now=datetime.now(timezone.utc),
        org_id=9_999_999,
    )
    assert created == 0, f"unknown org_id should be fail-closed empty: created={created}"
    assert notifications == 0


# ---------------------------------------------------------------------------
# Issue 14: _human_done_digest_rows three-channel OR (unit/lease/tenant also visible)
# ---------------------------------------------------------------------------


def test_issue14_human_done_three_channel_scope(db_session: Session):
    from app.services.operations.quick import _human_done_digest_rows
    ensure_default_org(db_session)
    org = db_session.query(Organization).first()
    prop = Property(organization_id=org.id, name="P", address="Unit Test St. 1", city="TestCity")
    db_session.add(prop)
    db_session.flush()
    unit = Unit(property_id=prop.id, unit_number="U1", monthly_rent=Decimal("800"))
    db_session.add(unit)
    db_session.flush()
    tenant = Tenant(organization_id=org.id, full_name="T")
    db_session.add(tenant)
    db_session.flush()
    lease = Lease(
        unit_id=unit.id, tenant_id=tenant.id,
        start_date=datetime.now(timezone.utc).date(),
        end_date=(datetime.now(timezone.utc) + timedelta(days=365)).date(),
        monthly_rent=Decimal("1000"), status=LeaseStatus.active,
    )
    db_session.add(lease)
    db_session.flush()
    now = datetime.now(timezone.utc)
    task_tenant = _make_task(
        title="tenant-channel only",
        status=OperationalTaskStatus.COMPLETED,
        tenant_id=tenant.id,
        completed_by=1, completed_at=now,
    )
    task_lease = _make_task(
        title="lease-channel only",
        status=OperationalTaskStatus.COMPLETED,
        lease_id=lease.id,
        completed_by=1, completed_at=now,
    )
    db_session.add_all([task_tenant, task_lease])
    db_session.commit()
    rows = _human_done_digest_rows(
        db_session, now=now, org_property_ids={prop.id},
    )
    keys = {r.get("business_dedupe_key") for r in rows}
    key_tenant = f"committed:{OperationalTaskType.FOLLOWUP.value}:{task_tenant.id}"
    key_lease = f"committed:{OperationalTaskType.FOLLOWUP.value}:{lease.id}"
    assert key_tenant in keys, "tenant channel missing from done digest scope gate"
    assert key_lease in keys, "lease channel missing from done digest scope gate"


# ---------------------------------------------------------------------------
# Issue 15: summary.py unbound tasks filtered by details.organization_id
# ---------------------------------------------------------------------------


def test_issue15_summary_unbound_task_org_link(db_session: Session):
    from app.services.operations.summary import build_operations_summary
    user = MagicMock(spec=User)
    user.role = UserRole.admin
    ensure_default_org(db_session)
    org = db_session.query(Organization).first()
    other_org = Organization(name="Other")
    db_session.add(other_org)
    db_session.flush()
    t_owned = _make_task(
        title="owned unbound",
        details={"organization_id": org.id},
    )
    t_other = _make_task(
        title="other org unbound",
        details={"organization_id": other_org.id},
    )
    db_session.add_all([t_owned, t_other])
    db_session.commit()
    summary = build_operations_summary(db_session, user, org_id=org.id)
    assert summary.pending_total == 1, (
        f"unbound cross-org tasks leak: expected 1 visible, got {summary.pending_total}"
    )


# ---------------------------------------------------------------------------
# Issue 3: move_out_workflow — begin_nested() exception propagates unchanged
# ---------------------------------------------------------------------------


def test_issue3_begin_nested_exception_not_swallowed(db_session: Session):
    from app.services.move_out_workflow import schedule_inspection
    ensure_default_org(db_session)
    org = db_session.query(Organization).first()
    prop = Property(organization_id=org.id, name="P3", address="Unit Test St. 3", city="TestCity")
    db_session.add(prop)
    db_session.flush()
    unit = Unit(property_id=prop.id, unit_number="U3", monthly_rent=Decimal("500"))
    db_session.add(unit)
    db_session.flush()
    tenant = Tenant(organization_id=org.id, full_name="T3")
    db_session.add(tenant)
    db_session.flush()
    lease = Lease(
        unit_id=unit.id, tenant_id=tenant.id,
        start_date=datetime.now(timezone.utc).date(),
        end_date=(datetime.now(timezone.utc) + timedelta(days=30)).date(),
        monthly_rent=Decimal("500"), status=LeaseStatus.active,
    )
    db_session.add(lease)
    db_session.commit()
    with patch.object(db_session, "begin_nested", side_effect=RuntimeError("tx-error")):
        with pytest.raises(RuntimeError, match="tx-error"):
            schedule_inspection(
                db_session,
                lease_id=lease.id,
                unit_id=unit.id,
                tenant_id=tenant.id,
                scheduled_at=datetime.now(timezone.utc),
                actor_id=1,
            )
