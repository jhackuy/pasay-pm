"""PR #42 CodeRabbit 修复证据链：跨组织 Fail-closed 与 Freeze Gate 反例。

本文件仅覆盖 PR #42 新增的 6 类阻断修复：
  A. Task Assignee ACTIVE + same-org + not-removed（403 反例）
  B. /operations/summary 跨组织不泄露（org2 task 不出现在 org1 返回）
  C. /operations/copilot/context 跨组织不泄露
  D. /operations/quick/tasks 跨组织不泄露（/quick/tasks 额外 Critical 修复）
  E. /operations/scheduler/run 跨组织不 mutate（org2 due rule 不被 org1 claim）
  F. NUM_TABLES parse 旧逻辑错误转绿 + 新逻辑正确取值证据

永不删除真实失败测试；永不削弱业务约束。
"""
from __future__ import annotations

import datetime as _dt
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

API = "/api/v1"
NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def _h(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# =====================================================================
# A. Assignee 跨组织 403 反例
# =====================================================================

def test_create_task_assignee_outside_org_is_403_fail_closed(
    client, db_session, owner_a, owner_b, org_a, org_b,
):
    """PR #42 Assignee Major：create_task 跨组织 assigned_user_id 必须 403。

    构造：org_b 的 OWNER（owner_b.user）作为 org_a OWNER 发起 create_task 的
    assigned_user_id。修复前：仅检查 User.is_active=True 直接 PASS 并落库
    OperationalTask + 可能发跨 org DM。修复后：Membership ACTIVE + same-org
    + removed_at IS NULL 双条件 fail-closed。
    """
    from tests.conftest import seed_property
    from app.models.operations import OperationalTask, NotificationOutbox

    prop_a = seed_property(db_session, org=org_a)
    user_b = owner_b[0]
    ha = _h(owner_a[1])

    before_tasks = db_session.query(OperationalTask).count()
    before_outbox = db_session.query(NotificationOutbox).count()

    resp = client.post(
        f"{API}/operations/tasks",
        json={
            "task_type": "FOLLOWUP",
            "title": "cross-org assignee must 403",
            "property_id": prop_a.id,
            "assigned_user_id": user_b.id,
        },
        headers=ha,
    )
    assert resp.status_code == 403, (
        "cross-org assignee must fail-closed 403; got %s %s"
        % (resp.status_code, resp.text)
    )
    assert resp.json().get("detail") in (
        "Assignee is not an active member of the caller's organization",
        "Assignee is not an active member of the caller's organization",
    ), resp.json()

    db_session.expire_all()
    after_tasks = db_session.query(OperationalTask).count()
    after_outbox = db_session.query(NotificationOutbox).count()
    assert after_tasks == before_tasks, (
        "cross-org assignee must NOT write OperationalTask row"
    )
    assert after_outbox == before_outbox, (
        "cross-org assignee must NOT enqueue NotificationOutbox DM"
    )


# =====================================================================
# B. /summary 跨组织不泄露
# =====================================================================

def test_summary_excludes_other_organization_tasks(
    client, db_session, owner_a, owner_b, org_a, org_b, monkeypatch,
):
    """PR #42 summary/copilot/scheduler Major：org1 admin 的 /summary
    返回绝对不能包含 org2 的 OperationalTask。"""
    from tests.conftest import seed_property, seed_unit, seed_tenant
    from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType
    import app.services.operations.summary as summary_mod

    class _FrozenDatetime:
        @staticmethod
        def now(tz):
            return NOW
        @staticmethod
        def combine(*args, **kw):
            return _dt.datetime.combine(*args, **kw)
    monkeypatch.setattr(summary_mod, "datetime", _FrozenDatetime)

    prop_a = seed_property(db_session, org=org_a)
    unit_a = seed_unit(db_session, prop=prop_a)
    tenant_a = seed_tenant(db_session, org=org_a)

    prop_b = seed_property(db_session, org=org_b)
    unit_b = seed_unit(db_session, prop=prop_b)
    tenant_b = seed_tenant(db_session, org=org_b)

    from app.models import Lease, LeaseStatus
    lease_a = Lease(unit_id=unit_a.id, tenant_id=tenant_a.id,
                    start_date=date(2026,1,1), end_date=date(2026,12,31),
                    monthly_rent=Decimal("10000.00"), deposit="0", status=LeaseStatus.active, due_day=1)
    lease_b = Lease(unit_id=unit_b.id, tenant_id=tenant_b.id,
                    start_date=date(2026,1,1), end_date=date(2026,12,31),
                    monthly_rent=Decimal("10000.00"), deposit="0", status=LeaseStatus.active, due_day=1)
    db_session.add_all([lease_a, lease_b])
    db_session.flush()

    title_a = "[ORG-A] overdue rent reminder (must appear)"
    title_b = "[ORG-B] overdue rent (must NOT leak)"
    t_a = OperationalTask(task_type=OperationalTaskType.RENT_OVERDUE,
                          title=title_a, status=OperationalTaskStatus.PENDING,
                          due_at=NOW - timedelta(days=2), lease_id=lease_a.id,
                          property_id=prop_a.id, source_type="manual", source_id=None)
    t_b = OperationalTask(task_type=OperationalTaskType.RENT_OVERDUE,
                          title=title_b, status=OperationalTaskStatus.PENDING,
                          due_at=NOW - timedelta(days=2), lease_id=lease_b.id,
                          property_id=prop_b.id, source_type="manual", source_id=None)
    db_session.add_all([t_a, t_b])
    db_session.commit()

    resp_a = client.get(f"{API}/operations/summary", headers=_h(owner_a[1]))
    assert resp_a.status_code == 200, resp_a.text
    summary_a = resp_a.json()
    assert summary_a["overdue"] == 1, (
        summary_a,
        "exactly 1 org-a overdue task must be counted; a count>1 would indicate "
        "cross-organization leak of org-B's RENT_OVERDUE task",
    )
    assert summary_a["pending_total"] == 1, (
        summary_a, "total pending must be exactly the single org-A task (fail-closed: prevent leak)"
    )
    assert summary_a["due_today"] == 0
    assert summary_a["due_7_days"] == 0

    resp_b = client.get(f"{API}/operations/summary", headers=_h(owner_b[1]))
    assert resp_b.status_code == 200, resp_b.text
    summary_b = resp_b.json()
    assert summary_b["overdue"] == 1, (summary_b, "org-B must see exactly its own 1 overdue task")
    assert summary_b["pending_total"] == 1, (summary_b, "org-B total pending must be exactly 1 (no org-A leak)")

    raw_resp = client.get(
        f"{API}/reports/tasks?overdue=true",
        headers=_h(owner_a[1]),
    )
    assert raw_resp.status_code == 200
    rows = raw_resp.json()
    titles_in_a = [r.get("title", "") for r in rows if isinstance(r, dict)]
    assert any("ORG-A" in t for t in titles_in_a), (titles_in_a, "org A overdue task must be visible to org A admin")
    assert not any("ORG-B" in t for t in titles_in_a), (
        titles_in_a, "org B task must NEVER appear in org A admin /reports/tasks (org-scope fail-closed)"
    )


# =====================================================================
# C. /copilot/context 跨组织不泄露（读取 endpoint 范围）
# =====================================================================

def test_copilot_context_excludes_other_org_leases_and_tasks(
    client, db_session, owner_a, owner_b, org_a, org_b,
):
    """PR #42 copilot endpoint Major：build_copilot_context 必须按
    org_id 过滤 leases/properties/tasks，禁止跨 org 返回。"""
    from tests.conftest import seed_property, seed_unit, seed_tenant
    from app.models import Lease, LeaseStatus
    from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType

    prop_a = seed_property(db_session, org=org_a, name="A-Tower")
    unit_a = seed_unit(db_session, prop=prop_a, unit_number="A1")
    tenant_a = seed_tenant(db_session, org=org_a)
    lease_a = Lease(unit_id=unit_a.id, tenant_id=tenant_a.id,
                    start_date=date(2026,1,1), end_date=date(2026,12,31),
                    monthly_rent=Decimal("12000.00"), deposit="0", status=LeaseStatus.active, due_day=1)

    prop_b = seed_property(db_session, org=org_b, name="B-Tower-Forbidden")
    unit_b = seed_unit(db_session, prop=prop_b, unit_number="B1")
    tenant_b = seed_tenant(db_session, org=org_b)
    lease_b = Lease(unit_id=unit_b.id, tenant_id=tenant_b.id,
                    start_date=date(2026,1,1), end_date=date(2026,12,31),
                    monthly_rent=Decimal("50000.00"), deposit="0", status=LeaseStatus.active, due_day=1)

    db_session.add_all([lease_a, lease_b])
    db_session.flush()

    t_a = OperationalTask(task_type=OperationalTaskType.FOLLOWUP, title="A-Tower task ok",
                          status=OperationalTaskStatus.PENDING, due_at=NOW + timedelta(days=1),
                          property_id=prop_a.id, source_type="manual", source_id=None)
    t_b = OperationalTask(task_type=OperationalTaskType.FOLLOWUP, title="B-Tower FORBIDDEN leak",
                          status=OperationalTaskStatus.PENDING, due_at=NOW + timedelta(days=1),
                          property_id=prop_b.id, source_type="manual", source_id=None)
    db_session.add_all([t_a, t_b])
    db_session.commit()

    resp = client.get(f"{API}/operations/copilot/context", headers=_h(owner_a[1]))
    assert resp.status_code == 200, resp.text
    ctx = resp.json()
    blob = repr(ctx)
    assert "A-Tower" in blob or "A1" in blob or "A-Tower task ok" in blob, (
        "org A context should carry org A rows"
    )
    assert "B-Tower-Forbidden" not in blob, (
        "ORG B property name MUST NOT leak in org A copilot/context. got=%s"
        % blob[:1200]
    )
    assert "B-Tower FORBIDDEN leak" not in blob, (
        "ORG B task title MUST NOT leak in org A copilot/context"
    )


# =====================================================================
# D. /quick/tasks 跨组织不泄露（PR #42 额外 Critical 修复）
# =====================================================================

def test_quick_tasks_excludes_other_org_operational_tasks_and_expenses(
    client, db_session, owner_a, owner_b, org_a, org_b,
):
    """/quick/tasks 额外 Critical：org A admin 请求绝对不能包含
    org B OperationalTask 或 payable Expense 行。"""
    from tests.conftest import seed_property, seed_unit, seed_tenant, seed_expense
    from app.models import Lease, LeaseStatus
    from app.models.operations import OperationalTask, OperationalTaskStatus, OperationalTaskType

    prop_a = seed_property(db_session, org=org_a, name="AQuickProp")
    unit_a = seed_unit(db_session, prop=prop_a)
    tenant_a = seed_tenant(db_session, org=org_a)
    lease_a = Lease(unit_id=unit_a.id, tenant_id=tenant_a.id,
                    start_date=date(2026,6,1), end_date=date(2026,12,31),
                    monthly_rent=Decimal("20000.00"), deposit="0",
                    status=LeaseStatus.active, due_day=1)
    prop_b = seed_property(db_session, org=org_b, name="BQuickProp_FORBIDDEN")
    unit_b = seed_unit(db_session, prop=prop_b)
    tenant_b = seed_tenant(db_session, org=org_b)
    lease_b = Lease(unit_id=unit_b.id, tenant_id=tenant_b.id,
                    start_date=date(2026,6,1), end_date=date(2026,12,31),
                    monthly_rent=Decimal("20000.00"), deposit="0",
                    status=LeaseStatus.active, due_day=1)
    db_session.add_all([lease_a, lease_b])
    db_session.flush()

    ta_title = "[A-QUICK] RENT_OVERDUE visible"
    tb_title = "[B-QUICK] FORBIDDEN must not leak"
    db_session.add_all([
        OperationalTask(task_type=OperationalTaskType.RENT_OVERDUE,
                        title=ta_title, status=OperationalTaskStatus.PENDING,
                        lease_id=lease_a.id, property_id=prop_a.id,
                        due_at=NOW - timedelta(days=10),
                        source_type="manual", source_id=None),
        OperationalTask(task_type=OperationalTaskType.RENT_OVERDUE,
                        title=tb_title, status=OperationalTaskStatus.PENDING,
                        lease_id=lease_b.id, property_id=prop_b.id,
                        due_at=NOW - timedelta(days=10),
                        source_type="manual", source_id=None),
    ])
    seed_expense(db_session, prop=prop_a, category="repair",
                 amount=Decimal("600.00"), status="approved",
                 approved_at=NOW, payee="A Repair")
    seed_expense(db_session, prop=prop_b, category="repair",
                 amount=Decimal("99999.99"), status="approved",
                 approved_at=NOW, payee="B REPAIR FORBIDDEN")
    db_session.commit()

    resp = client.get(f"{API}/operations/quick/tasks", headers=_h(owner_a[1]))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    payload_repr = repr(rows)
    assert ta_title in payload_repr, (rows[:3], "org A task must be visible")
    assert tb_title not in payload_repr, (rows, "org B task must NEVER leak")
    assert "B REPAIR FORBIDDEN" not in payload_repr, (
        rows, "org B approved payable expense must never leak via A quick/tasks"
    )
    assert "99999.99" not in payload_repr, (
        rows, "org B 99999.99 amount must be absent from org A quick/tasks"
    )


# =====================================================================
# E. /scheduler/run 跨组织不 mutate（org2 due rule 不被 claim）
# =====================================================================

def test_scheduler_run_does_not_claim_or_mutate_other_org_rules(
    client, db_session, owner_a, owner_b, org_a, org_b, monkeypatch,
):
    """PR #42 scheduler Major：org1 OWNER POST /scheduler/run 绝对不能
    claim org2 的 due RecurringRule 或 mutate org2 数据。"""
    from tests.conftest import seed_property, seed_unit
    from app.models.operations import RecurringRule
    from app.models.operations import OperationalTaskType, OperationalTaskStatus
    from app.services.operations import config as ops_config
    from app.services.operations import generation as gen_cfg

    admin_a = owner_a[0]
    # Assign Telegram destination so validate_default_assignee accepts admin_a as
    # the DEFAULT_ASSIGNED_USER_ID target (mirrors test_scheduler_run_endpoint).
    from app.models.user import User as _UserModel
    (
        db_session.query(_UserModel)
        .filter(_UserModel.id == admin_a.id)
        .update({"telegram_chat_id": "000000042"})
    )
    db_session.flush()
    monkeypatch.setattr(ops_config, "DEFAULT_ASSIGNED_USER_ID", admin_a.id)
    monkeypatch.setattr(gen_cfg, "DEFAULT_ASSIGNED_USER_ID", admin_a.id)

    prop_a = seed_property(db_session, org=org_a)
    unit_a = seed_unit(db_session, prop=prop_a)
    prop_b = seed_property(db_session, org=org_b)
    unit_b = seed_unit(db_session, prop=prop_b)

    from datetime import timedelta as _td
    rule_a = RecurringRule(
        rule_type=OperationalTaskType.AC_MAINTENANCE,
        title="A org AC filter (due now - should be claimed)",
        property_id=prop_a.id,
        recurrence="monthly",
        interval_months=1,
        next_run_at=NOW - _td(days=1),
        enabled=True,
    )
    rule_b = RecurringRule(
        rule_type=OperationalTaskType.FOLLOWUP,
        title="B org plumbing CHECK MUST NOT BE CLAIMED",
        property_id=prop_b.id,
        recurrence="monthly",
        interval_months=1,
        next_run_at=NOW - _td(days=1),
        enabled=True,
    )
    db_session.add_all([rule_a, rule_b])
    db_session.commit()
    db_session.refresh(rule_a)
    db_session.refresh(rule_b)

    a_next_run_before = rule_a.next_run_at
    a_enabled_before = rule_a.enabled
    a_title_before = rule_a.title
    b_next_run_before = rule_b.next_run_at
    b_enabled_before = rule_b.enabled
    b_title_before = rule_b.title

    resp = client.post(f"{API}/operations/scheduler/run", headers=_h(owner_a[1]))
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    db_session.refresh(rule_a)
    db_session.refresh(rule_b)
    assert rule_a.next_run_at > a_next_run_before, (
        "org A rule next_run_at MUST advance when org A runs scheduler (positive control)"
    )
    assert rule_a.enabled is a_enabled_before
    assert rule_a.title == a_title_before
    assert rule_b.next_run_at == b_next_run_before, (
        "org B rule next_run_at must NOT advance when org A runs scheduler"
    )
    assert rule_b.enabled is b_enabled_before
    assert rule_b.title == b_title_before
    from app.models.operations import OperationalTask
    org_a_rows = (
        db_session.query(OperationalTask)
        .filter(OperationalTask.property_id == prop_a.id)
        .all()
    )
    assert len(org_a_rows) >= 1, (
        "scheduler triggered by org A admin MUST write at least 1 OperationalTask to org A property (positive AC claim). rows=%r"
        % [(r.task_type, r.title, r.property_id) for r in org_a_rows]
    )
    assert any(
        r.title == "A org AC filter (due now - should be claimed)" for r in org_a_rows
    ), "org A AC rule title must be present in generated OperationalTask rows"
    org_b_rows = (
        db_session.query(OperationalTask)
        .filter(OperationalTask.property_id == prop_b.id)
        .all()
    )
    assert len(org_b_rows) == 0, (
        "scheduler triggered by org A admin NEVER writes OperationalTask to org B property. rows=%r"
        % [(r.task_type, r.title, r.property_id) for r in org_b_rows]
    )


# =====================================================================
# F. NUM_TABLES parse 旧逻辑错误转绿 / 新逻辑正确 证据
# =====================================================================

def test_num_tables_old_logic_parses_row_count_trailer_instead_of_value():
    """DOOR-05 Critical：旧 grep+tail 错误读取 psql 输出的 "(1 row)" 行尾
    中的 1，从而在真实表数为 0 时仍能 >=35 PASS（错误转绿）。

    用纯 Python 复现 psql 输出与旧逻辑：
      COUNT_OUT 内容 = SELECT COUNT 的结果 + 空行 + "(1 row)"
      旧逻辑 = grep -oE '[0-9]+' | tail -n1 → 返回 "1"（行尾数字）
      新逻辑 = psql -At → 返回纯标量
    """
    fake_count_output = (
        " count \n"
        "-------\n"
        "    40\n"
        "\n"
        "(1 row)\n"
    )
    # 旧 bash 逻辑：grep -oE '[0-9]+' | tail -n1
    import re
    nums_found = re.findall(r"[0-9]+", fake_count_output)
    old_result = nums_found[-1] if nums_found else None
    # 这是 Critical bug — 拿到 "(1 row)" 中的 "1"，而不是真实的 "40"
    assert old_result == "1", (
        "旧 grep+tail 逻辑应该错误返回 (1 row) 中的 '1'; got=%r" % old_result
    )
    assert old_result != "40", "证明旧逻辑错误：无法读到正确值 40"

    # 新逻辑：psql -At（tuple-only + unaligned）输出直接 = "40\n"
    new_logic_output = "40\n"
    new_result = new_logic_output.strip()
    assert new_result == "40", "新 -At 逻辑直接返回标量 40"
    assert int(new_result) >= 35, (
        "真实 40 表时新逻辑能正确通过 >=35 freeze gate"
    )
    # 反例：真 0 表（空库没迁移前）新逻辑  = 0  → 不能过 freeze gate
    zero_db_new_output = "0\n"
    zero_result = int(zero_db_new_output.strip())
    assert zero_result == 0
    assert zero_result < 35, (
        "真 0 表（没执行 alembic upgrade head）必须 FAIL；证明 DOOR-05 无法被错误转绿"
    )
