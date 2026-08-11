"""Business-source task generation (V1.2 Phase B).

Scans the source-of-truth business tables and atomically creates
``operational_tasks`` rows using INSERT ... ON CONFLICT DO NOTHING against
the partial unique index ``uq_operational_tasks_active_dedupe`` (dedupe_key
unique while PENDING). New tasks get a notification_outbox row in the SAME
transaction (at-least-once delivery).

Financial status is NEVER written here — incomes/expenses/settlements are
only read; their real state transitions stay in the V1.1 routers.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.commission import CommissionSettlement, CommissionSettlementStatus
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.property import Unit
from app.models.tenant import Tenant
from app.services.audit import record_audit, serialize_row
from app.services.operations.rent_math import covered_periods, lease_periods
from app.services.operations.config import (
    APPROVAL_PENDING_AFTER_DAYS,
    DEFAULT_ASSIGNED_USER_ID,
    LEASE_EXPIRY_WINDOW_DAYS,
    NOTIFY_CHANNEL_TELEGRAM,
    PAYMENT_PENDING_AFTER_DAYS,
    RENT_DUE_ADVANCE_DAYS,
    SETTLEMENT_PENDING_AFTER_DAYS,
)
from app.services.operations.outbox import enqueue_notification, resolve_recipient
from app.services.operations.reconcile import auto_transition

BUSINESS_SOURCE_TYPES = frozenset({"lease", "expense", "commission_settlement"})


def _notification_message(task: OperationalTask) -> str:
    details = task.details or {}
    lines = [
        "🔔 待办提醒",
        f"#{task.id} · {task.task_type.value}",
        f"{task.title}",
    ]
    amount = details.get("amount")
    if amount is not None:
        lines.append(f"金额：{amount}")
    period = details.get("period") or details.get("periods")
    if period:
        lines.append(f"账期：{period}")
    lines.append(f"到期：{task.due_at:%Y-%m-%d %H:%M}")
    return "\n".join(lines)


def _insert_task_on_conflict_do_nothing(db: Session, *, fields: dict) -> OperationalTask | None:
    """Atomic create against the PENDING dedupe index; returns None when a
    conflicting active task already exists (or dedupe_key is None)."""
    if fields.get("dedupe_key") is None:
        obj = OperationalTask(**fields)
        db.add(obj)
        db.flush()
        return obj
    stmt = (
        pg_insert(OperationalTask)
        .values(**fields)
        .on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=text("status = 'PENDING'"),
        )
        .returning(OperationalTask.id)
    )
    row = db.execute(stmt).first()
    if row is None:
        return None
    return db.get(OperationalTask, row[0])


def _enqueue_for_task(db: Session, task: OperationalTask) -> bool:
    """Outbox row for one new task (same transaction). Returns True when
    enqueued, False when no recipient is resolvable."""
    recipient = resolve_recipient(db, task.assigned_user_id)
    if recipient is None:
        return False
    return enqueue_notification(
        db,
        task_id=task.id,
        channel=NOTIFY_CHANNEL_TELEGRAM,
        recipient=recipient,
        payload={
            "task_id": task.id,
            "task_type": task.task_type.value,
            "title": task.title,
            "due_at": task.due_at.isoformat(),
            "message": _notification_message(task),
        },
        dedupe_key=f"task:{task.id}:{NOTIFY_CHANNEL_TELEGRAM}:{recipient}",
    )


def _register_task(
    db: Session, *, fields: dict, now: datetime, actor_id: int | None = None
) -> tuple[OperationalTask | None, bool]:
    """Create task + audit(task_created) + outbox in one transaction.

    Business-source tasks with no explicit assignee fall back to
    ``DEFAULT_ASSIGNED_USER_ID`` so proactive notifications get a recipient;
    recurring-rule tasks keep the rule's assignee as-is.

    Returns (task_or_None, notification_enqueued).
    """
    fields = dict(fields)
    if (
        fields.get("source_type") in BUSINESS_SOURCE_TYPES
        and fields.get("assigned_user_id") is None
    ):
        fields["assigned_user_id"] = DEFAULT_ASSIGNED_USER_ID
    task = _insert_task_on_conflict_do_nothing(db, fields=fields)
    if task is None:
        return None, False
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action="task_created",
        actor_id=actor_id,  # None = system / scheduler
        new_value=serialize_row(task),
    )
    return task, _enqueue_for_task(db, task)


def create_operational_task(
    db: Session, *, fields: dict, now: datetime | None = None, actor_id: int | None = None
) -> tuple[OperationalTask | None, bool]:
    """Public seam for human-confirmed (V1.2.2 C2 copilot) task creation.

    Same atomic create + audit + outbox in ONE transaction as the scheduler's
    private ``_register_task`` — there is exactly ONE write path for
    ``operational_tasks``. ``actor_id`` records the human who confirmed the
    action (None = system/scheduler). Returns ``(task_or_None, enqueued)``;
    ``task_or_None`` is None when a PENDING task with the same ``dedupe_key``
    already exists (DB dedupe boundary = at-most-one active followup).
    """
    now = now or datetime.now(timezone.utc)
    return _register_task(db, fields=fields, now=now, actor_id=actor_id)


def _supersede_rent_due(db: Session, lease_id: int, now: datetime) -> None:
    """Complete PENDING RENT_DUE tasks for a lease that just became overdue
    (superseded by the RENT_OVERDUE task) — keeps the board noise-free."""
    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.status == OperationalTaskStatus.PENDING,
            OperationalTask.task_type == OperationalTaskType.RENT_DUE,
            OperationalTask.source_type == "lease",
            OperationalTask.source_id == lease_id,
        )
        .all()
    )
    for task in tasks:
        auto_transition(
            db, task, to=OperationalTaskStatus.COMPLETED, now=now,
            reason="superseded_by_rent_overdue",
        )


def _rent_task_details(lease: Lease, unit: Unit | None, tenant: Tenant | None, extra: dict) -> dict:
    details = {
        "lease_id": lease.id,
        "amount": str(lease.monthly_rent),
        "unit_number": unit.unit_number if unit else None,
        "tenant_name": tenant.full_name if tenant else None,
    }
    details.update(extra)
    return details


def generate_business_tasks(db: Session, *, now: datetime) -> tuple[int, int]:
    """Create tasks from business sources. Returns (tasks_created, notifications_enqueued)."""
    created = 0
    notifications = 0
    today = now.date()

    leases = (
        db.query(Lease)
        .filter(Lease.status == LeaseStatus.active, Lease.deleted_at.is_(None))
        .all()
    )
    lease_ids = [lease.id for lease in leases]
    confirmed_by_lease: dict[int, list[Income]] = {}
    if lease_ids:
        for income in (
            db.query(Income)
            .filter(Income.lease_id.in_(lease_ids), Income.status == IncomeStatus.confirmed)
            .all()
        ):
            confirmed_by_lease.setdefault(income.lease_id, []).append(income)

    units = {u.id: u for u in db.query(Unit).all()}
    tenants = {t.id: t for t in db.query(Tenant).all()}

    # --- RENT_DUE / RENT_OVERDUE -------------------------------------------------
    for lease in leases:
        unit = units.get(lease.unit_id)
        tenant = tenants.get(lease.tenant_id)
        periods = lease_periods(lease)
        covered = covered_periods(lease, periods, confirmed_by_lease.get(lease.id, []))
        window_end = today + timedelta(days=RENT_DUE_ADVANCE_DAYS)
        due_periods = [(m, d) for m, d in periods if d <= window_end]
        overdue = [(m, d) for m, d in due_periods if d < today and m not in covered]
        upcoming = [(m, d) for m, d in due_periods if d >= today and m not in covered]

        if overdue:
            oldest_due = overdue[0][1]
            amount = _d2(Decimal(str(lease.monthly_rent)) * len(overdue))
            task, enqueued = _register_task(
                db,
                now=now,
                fields={
                    "task_type": OperationalTaskType.RENT_OVERDUE,
                    "title": f"租金逾期 · {len(overdue)}期",
                    "property_id": unit.property_id if unit else None,
                    "tenant_id": lease.tenant_id,
                    "lease_id": lease.id,
                    "source_type": "lease",
                    "source_id": lease.id,
                    "priority": OperationalTaskPriority.high,
                    "status": OperationalTaskStatus.PENDING,
                    "due_at": datetime.combine(oldest_due, time.min, tzinfo=now.tzinfo),
                    "dedupe_key": f"lease:{lease.id}:RENT_OVERDUE",
                    "details": _rent_task_details(
                        lease, unit, tenant,
                        {"periods": [m for m, _ in overdue], "total_outstanding": str(amount)},
                    ),
                },
            )
            if task is not None:
                created += 1
                notifications += 1 if enqueued else 0
                _supersede_rent_due(db, lease.id, now)

        for month, due in upcoming:
            task, enqueued = _register_task(
                db,
                now=now,
                fields={
                    "task_type": OperationalTaskType.RENT_DUE,
                    "title": f"租金到期 {month}",
                    "property_id": unit.property_id if unit else None,
                    "tenant_id": lease.tenant_id,
                    "lease_id": lease.id,
                    "source_type": "lease",
                    "source_id": lease.id,
                    "priority": OperationalTaskPriority.medium,
                    "status": OperationalTaskStatus.PENDING,
                    "due_at": datetime.combine(due, time.min, tzinfo=now.tzinfo),
                    "dedupe_key": f"lease:{lease.id}:RENT_DUE:{month}",
                    "details": _rent_task_details(lease, unit, tenant, {"period": month}),
                },
            )
            if task is not None:
                created += 1
                notifications += 1 if enqueued else 0

    # --- LEASE_EXPIRING ----------------------------------------------------------
    expiry_window_end = today + timedelta(days=LEASE_EXPIRY_WINDOW_DAYS)
    for lease in leases:
        if not (today <= lease.end_date <= expiry_window_end):
            continue
        unit = units.get(lease.unit_id)
        task, enqueued = _register_task(
            db,
            now=now,
            fields={
                "task_type": OperationalTaskType.LEASE_EXPIRING,
                "title": f"租约即将到期 {lease.end_date.isoformat()}",
                "property_id": unit.property_id if unit else None,
                "tenant_id": lease.tenant_id,
                "lease_id": lease.id,
                "source_type": "lease",
                "source_id": lease.id,
                "priority": OperationalTaskPriority.medium,
                "status": OperationalTaskStatus.PENDING,
                "due_at": datetime.combine(lease.end_date, time.min, tzinfo=now.tzinfo),
                "dedupe_key": f"lease:{lease.id}:LEASE_EXPIRING",
                "details": {"lease_id": lease.id, "end_date": lease.end_date.isoformat()},
            },
        )
        if task is not None:
            created += 1
            notifications += 1 if enqueued else 0

    # --- APPROVAL_PENDING / PAYMENT_PENDING (expenses, read-only) ----------------
    approval_cutoff = now - timedelta(days=APPROVAL_PENDING_AFTER_DAYS)
    pending_expenses = (
        db.query(Expense)
        .filter(Expense.status == ExpenseStatus.pending, Expense.created_at <= approval_cutoff)
        .all()
    )
    for expense in pending_expenses:
        task, enqueued = _register_task(
            db,
            now=now,
            fields={
                "task_type": OperationalTaskType.APPROVAL_PENDING,
                "title": f"待审批支出 #{expense.id}",
                "source_type": "expense",
                "source_id": expense.id,
                "priority": OperationalTaskPriority.medium,
                "status": OperationalTaskStatus.PENDING,
                "due_at": expense.due_date and datetime.combine(expense.due_date, time.min, tzinfo=now.tzinfo)
                or expense.created_at,
                "dedupe_key": f"expense:{expense.id}:APPROVAL_PENDING",
                "details": {
                    "expense_id": expense.id,
                    "amount": str(expense.amount),
                    "category": expense.category,
                    "payee": expense.payee,
                },
            },
        )
        if task is not None:
            created += 1
            notifications += 1 if enqueued else 0

    payment_cutoff = now - timedelta(days=PAYMENT_PENDING_AFTER_DAYS)
    approved_expenses = (
        db.query(Expense)
        .filter(
            Expense.status == ExpenseStatus.approved,
            (Expense.approved_at.isnot(None)) & (Expense.approved_at <= payment_cutoff),
        )
        .all()
    )
    for expense in approved_expenses:
        task, enqueued = _register_task(
            db,
            now=now,
            fields={
                "task_type": OperationalTaskType.PAYMENT_PENDING,
                "title": f"待付款支出 #{expense.id}",
                "source_type": "expense",
                "source_id": expense.id,
                "priority": OperationalTaskPriority.high,
                "status": OperationalTaskStatus.PENDING,
                "due_at": expense.due_date and datetime.combine(expense.due_date, time.min, tzinfo=now.tzinfo)
                or expense.approved_at or now,
                "dedupe_key": f"expense:{expense.id}:PAYMENT_PENDING",
                "details": {
                    "expense_id": expense.id,
                    "amount": str(expense.amount),
                    "category": expense.category,
                    "payee": expense.payee,
                },
            },
        )
        if task is not None:
            created += 1
            notifications += 1 if enqueued else 0

    # --- SETTLEMENT_PENDING -------------------------------------------------------
    settlement_cutoff = now - timedelta(days=SETTLEMENT_PENDING_AFTER_DAYS)
    pending_settlements = (
        db.query(CommissionSettlement)
        .filter(
            CommissionSettlement.status == CommissionSettlementStatus.pending,
            CommissionSettlement.created_at <= settlement_cutoff,
        )
        .all()
    )
    for settlement in pending_settlements:
        task, enqueued = _register_task(
            db,
            now=now,
            fields={
                "task_type": OperationalTaskType.SETTLEMENT_PENDING,
                "title": f"待确认佣金结算 #{settlement.id}",
                "source_type": "commission_settlement",
                "source_id": settlement.id,
                "assigned_user_id": settlement.agent_id,
                "priority": OperationalTaskPriority.medium,
                "status": OperationalTaskStatus.PENDING,
                "due_at": settlement.created_at,
                "dedupe_key": f"commission_settlement:{settlement.id}:SETTLEMENT_PENDING",
                "details": {
                    "settlement_id": settlement.id,
                    "amount": str(settlement.computed_amount),
                    "agent_id": settlement.agent_id,
                },
            },
        )
        if task is not None:
            created += 1
            notifications += 1 if enqueued else 0

    return created, notifications


def generate_rule_task(db: Session, rule, *, now: datetime) -> tuple[OperationalTask | None, bool]:
    """Generate one task for a claimed recurring rule and advance its
    next_run_at. Returns (task_or_None, notification_enqueued)."""
    period_key = period_key_for(rule, now)
    details = dict(rule.details or {})
    details["rule_id"] = rule.id
    details["period"] = period_key
    task, enqueued = _register_task(
        db,
        now=now,
        fields={
            "task_type": rule.rule_type,
            "title": rule.title,
            "description": rule.description,
            "property_id": rule.property_id,
            "source_type": "recurring_rule",
            "source_id": rule.id,
            "assigned_user_id": rule.assigned_user_id,
            "priority": OperationalTaskPriority.medium,
            "status": OperationalTaskStatus.PENDING,
            "due_at": rule.next_run_at,
            "dedupe_key": f"recurring:{rule.id}:{period_key}",
            "details": details,
        },
    )
    if task is not None:
        rule.next_run_at = advance_next_run(rule, rule.next_run_at)
    return task, enqueued


def period_key_for(rule, run_at: datetime) -> str:
    """Business period key for the dedupe fingerprint."""
    year, month = run_at.year, run_at.month
    if rule.recurrence.value == "quarterly":
        return f"{year:04d}-Q{(month - 1) // 3 + 1}"
    if rule.recurrence.value == "yearly":
        return f"{year:04d}"
    return f"{year:04d}-{month:02d}"


def advance_next_run(rule, from_at: datetime) -> datetime:
    """Push next_run_at forward by the rule's recurrence interval."""
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}.get(rule.recurrence.value)
    if months is None:  # fixed_interval
        months = rule.interval_months or 1
    from app.services.dates import add_months
    return datetime.combine(add_months(from_at.date(), months), from_at.time(), tzinfo=from_at.tzinfo)


def _d2(value) -> Decimal:
    """Normalize a Numeric (or string/Decimal) value to 2dp."""
    return Decimal(str(value)).quantize(Decimal("0.01"))
