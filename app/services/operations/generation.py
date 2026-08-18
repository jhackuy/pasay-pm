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

import secrets
import time as _time
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
from app.models.user import User, UserRole
from app.services.audit import record_audit, serialize_row
from app.services.operations.rent_math import covered_periods, lease_periods
from app.services.operations.config import (
    APPROVAL_PENDING_AFTER_DAYS,
    DEFAULT_ASSIGNED_USER_ID,
    LEASE_EXPIRY_WINDOW_DAYS,
    NOTIFY_CHANNEL_TELEGRAM,
    PAYMENT_PENDING_AFTER_DAYS,
    RENT_DUE_ADVANCE_DAYS,
    SECRETARY_ASSIGNEE_ID,
    SETTLEMENT_PENDING_AFTER_DAYS,
)
from app.services.operations.outbox import enqueue_notification, resolve_recipient
from app.services.operations.reconcile import auto_transition

BUSINESS_SOURCE_TYPES = frozenset({"lease", "expense", "commission_settlement"})

# Placeholder sentinels that must never appear in a user-visible task title
# (EXPENSE-UX-FIX-001 Bug 2). Legacy rows may store `??` in expense.category;
# the title is built from the REAL purpose mapping (category -> description ->
# payee, sentinels dropped), never from the raw DB value.
_TITLE_SENTINELS = frozenset({"??", "?", "--", "none", "null", "n/a", "na", "unknown"})


def _clean_title_part(value) -> str:
    """First truthful fragment (category -> description -> payee) with
    placeholder sentinels dropped; empty string when nothing remains."""
    for field in (value.category, value.description, getattr(value, "payee", None)):
        if not field:
            continue
        text = " ".join(str(field).split())
        if text and text.lower() not in _TITLE_SENTINELS:
            return text
    return ""


def _expense_task_title(prefix: str, expense) -> str:
    """Human task title like `待付款支出 · 维修`; the purpose part is the
    cleaned real purpose, and is omitted entirely when no truthful purpose
    exists (a bare `待付款支出` is still actionable)."""
    purpose = _clean_title_part(expense)
    return f"{prefix} · {purpose}" if purpose else prefix


def secretary_assignee_id() -> int | None:
    """AI-OPS-FOUNDATION-001 §4: routine operational work (rent collection,
    lease follow-up) is assigned to the SECRETARY, never the Owner. Falls back
    to the default (Owner) only when no secretary is configured."""
    if SECRETARY_ASSIGNEE_ID is not None:
        return SECRETARY_ASSIGNEE_ID
    return DEFAULT_ASSIGNED_USER_ID


def _money(value) -> str:
    """3500.00 -> ₱3,500 ; 3500.50 -> ₱3,500.50 ; -500 -> -₱500."""
    from decimal import Decimal
    d = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    sign = "-" if d < 0 else ""
    s = format(abs(d), "f")
    if "." in s:
        int_part, _, frac = s.partition(".")
        s = int_part if frac == "00" else f"{int_part}.{frac}"
    int_part, sep, frac = s.partition(".")
    int_part = f"{int(int_part):,}"
    return f"{sign}₱{int_part}" + (f".{frac}" if frac else "")


def _task_locale(db: Session | None, user_id: int | None) -> str:
    if db is None or user_id is None:
        return "zh"
    user = db.get(User, user_id)
    if user is None:
        return "zh"
    return "zh" if user.role == UserRole.admin else "en"


def _actor_label(db: Session | None, user_id: int | None) -> str:
    if db is None or user_id is None:
        return "Unknown"
    user = db.get(User, user_id)
    if user is None:
        return "Unknown"
    return "Owner" if user.role == UserRole.admin else "Secretary"


def _period_label(periods, locale: str) -> str:
    if isinstance(periods, list):
        return "、".join(str(p) for p in periods) if locale == "zh" else ", ".join(str(p) for p in periods)
    return str(periods or "")


def _overdue_days(task: OperationalTask) -> int | None:
    if not task.due_at:
        return None
    days = (datetime.now(task.due_at.tzinfo).date() - task.due_at.date()).days
    return max(days, 0)


def _last_followup_label(details: dict, locale: str) -> str:
    last_followup = (
        details.get("last_followup_at")
        or details.get("followed_up_at")
        or details.get("last_contact_at")
    )
    if not last_followup:
        return "尚未催租" if locale == "zh" else "Not yet followed up"
    return str(last_followup).replace("T", " ")[:16]


def _localized_next_action(task: OperationalTask, locale: str) -> str:
    action = str(task.next_action or "").strip()
    if task.task_type == OperationalTaskType.RENT_OVERDUE:
        if locale == "zh":
            if action == "Secretary to contact tenant for overdue rent.":
                return "Secretary 联系租客催收"
        else:
            if action:
                return action
    return action


def _notification_message(task: OperationalTask, locale: str = "zh", *, current_actor: str | None = None) -> str:
    """Humanized proactive notification (V1.3): '待办提醒' + human title +
    amount/period/due. No task_type enum values, no internal #ids."""
    details = task.details or {}
    if task.task_type == OperationalTaskType.RENT_OVERDUE:
        periods = details.get("periods") or details.get("period")
        amount = details.get("amount") or details.get("total_outstanding")
        overdue_days = _overdue_days(task)
        actor = current_actor or ""
        next_action = _localized_next_action(task, locale)
        if locale == "zh":
            lines = ["🔔 租金逾期"]
            if details.get("unit_number"):
                lines.append(f"房号：{details.get('unit_number')}")
            if details.get("tenant_name"):
                lines.append(f"租客：{details.get('tenant_name')}")
            if amount is not None:
                suffix = f" · {len(periods)}期" if isinstance(periods, list) and periods else ""
                lines.append(f"欠款：{_money(amount)}{suffix}")
            if periods:
                lines.append(f"账期：{_period_label(periods, locale)}")
            if overdue_days is not None:
                lines.append(f"逾期：{overdue_days}天")
            lines.append(f"最近催租：{_last_followup_label(details, locale)}")
            if actor and actor != "Unknown":
                lines.append(f"当前处理：{actor}")
            if next_action:
                lines.append("")
                lines.append(f"下一步：{next_action}")
            return "\n".join(lines)
        lines = ["🔔 Rent Overdue"]
        if details.get("unit_number"):
            lines.append(f"Unit: {details.get('unit_number')}")
        if details.get("tenant_name"):
            lines.append(f"Tenant: {details.get('tenant_name')}")
        if amount is not None:
            period_suffix = ""
            if isinstance(periods, list) and periods:
                unit = "period" if len(periods) == 1 else "periods"
                period_suffix = f" · {len(periods)} {unit}"
            lines.append(f"Outstanding: {_money(amount)}{period_suffix}")
        if periods:
            lines.append(f"Rent period: {_period_label(periods, locale)}")
        if overdue_days is not None:
            unit = "day" if overdue_days == 1 else "days"
            lines.append(f"Overdue: {overdue_days} {unit}")
        lines.append(f"Last follow-up: {_last_followup_label(details, locale)}")
        if current_actor:
            lines.append(f"Current actor: {current_actor}")
        if next_action:
            lines.append("")
            lines.append(f"Next action: {next_action}")
        return "\n".join(lines)
    lines = ["🔔 待办提醒" if locale == "zh" else "🔔 Task Reminder", task.title]
    amount = details.get("amount") or details.get("total_outstanding")
    if amount is not None:
        lines.append(f"{'金额' if locale == 'zh' else 'Amount'}：{_money(amount)}")
    period = details.get("period") or details.get("periods")
    if period:
        lines.append(f"{'账期' if locale == 'zh' else 'Period'}：{_period_label(period, locale)}")
    if task.due_at:
        lines.append(f"{'到期' if locale == 'zh' else 'Due'}：{task.due_at:%Y-%m-%d %H:%M}")
    return "\n".join(lines)


def _task_navigation_reply_markup(task: OperationalTask, locale: str) -> dict:
    """Notification CTA projects the existing task truth; no reminder-only flow."""
    detail_label = "✅ 查看待办" if locale == "zh" else "✅ View Task"
    return {
        "inline_keyboard": [
            [{"text": detail_label, "callback_data": f"v1:tkd:ops:{task.id}"}]
        ]
    }


def _expense_reply_markup(expense_id: int, has_receipt: bool) -> dict:
    """Inline keyboard dict for an expense notification (V1.3): approve/reject
    callbacks use the bot's v1:exa/exr:<id>:<nonce>:<ts> slot layout (the
    middle ref slot stays empty: v1:exa:<id>::<nonce>:<ts> so the bot's fixed
    decoder parses nonce/ts correctly); the detail button is a plain
    v1:exd:<id> and its label depends on whether a real receipt exists."""
    nonce = secrets.token_hex(4)
    ts = int(_time.time())
    detail_label = "📎 查看凭证" if has_receipt else "查看详情"
    return {
        "inline_keyboard": [
            [
                {"text": "✅ 批准", "callback_data": f"v1:exa:{expense_id}::{nonce}:{ts}"},
                {"text": "❌ 拒绝", "callback_data": f"v1:exr:{expense_id}::{nonce}:{ts}"},
            ],
            [{"text": detail_label, "callback_data": f"v1:exd:{expense_id}"}],
        ]
    }


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


def _get_active_task_by_dedupe(db: Session, dedupe_key: str) -> OperationalTask | None:
    """One ACTIVE (PENDING or IN_PROGRESS) task with this dedupe_key.

    The DB partial index only protects PENDING rows; an IN_PROGRESS row must
    also be treated as "the same logical issue" so a reminder never creates a
    duplicate sibling while a human is already working it."""
    if not dedupe_key:
        return None
    return (
        db.query(OperationalTask)
        .filter(
            OperationalTask.dedupe_key == dedupe_key,
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .first()
    )


_REFRESHABLE_FIELDS = (
    "title", "description", "details", "due_at", "priority",
    "next_action", "next_check_at",
)


def _refresh_active_task(
    db: Session,
    task: OperationalTask,
    *,
    fields: dict,
    now: datetime,
    actor_id: int | None,
    notification_message: str | None,
    reply_markup: dict | None,
    proactive: bool = False,
) -> tuple[OperationalTask, bool]:
    """Remind/update the SAME logical task (dedupe_key match) instead of
    creating a duplicate active task (AI-OPS-FOUNDATION-001 §2: one business
    issue = one active task; a new reminder updates/reminds the same task).

    Only reminder-relevant fields are refreshed; status/assignee never change
    here.

    CONVERGENCE-003 §1.3/§1.5 daily cadence: for PROACTIVE reminders still
    PENDING, every scheduler pass attempts ONE logical reminder (generation
    bumped) and the persistent daily dedupe decides whether today's slot is
    still free — same-day repeat scans are suppressed, while the NEXT
    Philippines day (item still incomplete) is allowed exactly one fresh
    reminder. Acknowledged (IN_PROGRESS) tasks never get another reminder.
    """
    old = serialize_row(task)
    changed = False
    for key in _REFRESHABLE_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if key == "details" and value is not None:
            merged = dict(task.details or {})
            merged.update(value)
            if merged != (task.details or {}):
                task.details = merged
                changed = True
            continue
        if getattr(task, key, None) != value:
            setattr(task, key, value)
            changed = True
    wants_reminder = proactive and task.status == OperationalTaskStatus.PENDING
    if not changed and not wants_reminder:
        return task, False
    task.reminder_generation = (task.reminder_generation or 0) + 1
    task.updated_at = now
    db.flush()
    if changed or wants_reminder:
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_reminded",
            actor_id=actor_id,  # None = system / scheduler
            changed_fields={
                "title": [old.get("title"), task.title],
                "reminder_generation": [old.get("reminder_generation", 0), task.reminder_generation],
            },
            old_value=old,
            new_value=serialize_row(task),
        )
    # A proactive reminder fires only while the task is still PENDING
    # (acknowledged/in-progress items never get another reminder), and the
    # daily dedupe decides whether today's slot is still free.
    should_enqueue = wants_reminder or (changed and not proactive)
    if not should_enqueue:
        return task, False
    enqueued = _enqueue_for_task(
        db, task,
        notification_message=notification_message,
        reply_markup=reply_markup,
        proactive=wants_reminder,
        now=now,
    )
    return task, enqueued


def _enqueue_for_task(
    db: Session,
    task: OperationalTask,
    *,
    notification_message: str | None = None,
    reply_markup: dict | None = None,
    proactive: bool = False,
    now: datetime | None = None,
) -> bool:
    """Outbox row for one new task (same transaction). Returns True when
    enqueued, False when no recipient is resolvable.

    ``notification_message`` is an OPT-IN explicit message override (the C2
    copilot executor renders an English secretary card); when omitted the
    Chinese ``_notification_message`` is used, so scheduler business tasks
    keep their existing message unchanged.

    The dedupe key embeds the reminder generation so a refreshed reminder
    (task_reminded) is a NEW logical notification even though the outbox
    ``uq_notification_outbox_dedupe`` index would otherwise swallow it.

    ``proactive=True`` (scheduler-generated reminders) additionally claims the
    persistent DAILY dedupe slot (TELEGRAM-OPS-UX-CONVERGENCE-003 §1.4): the
    same business object + recipient + PH local date + reminder type is sent
    at most once per Philippines natural day — a high-frequency scheduler scan
    never becomes high-frequency sending. Human-confirmed actions
    (``create_operational_task``) are reactive, not proactive, and pass False.
    ``now`` is the pass timestamp (same one the scheduler used); the PH local
    date is derived from it so a test or replay pass dedupes consistently.
    """
    try:
        recipient = resolve_recipient(db, task.assigned_user_id)
    except LookupError:
        # AI-OPS-FOUNDATION-001 §1: the TASK is the source of truth — an
        # unresolvable notification recipient must never block task creation.
        return False
    if recipient is None:
        return False
    if proactive:
        from app.services.operations.daily_dedup import claim_daily_dedup

        if not claim_daily_dedup(
            db,
            business_key=task.dedupe_key,
            task_id=task.id,
            recipient=recipient,
            reminder_type=task.task_type.value,
            now=now,
        ):
            return False  # already reminded today -> high-frequency scan, no send
    details = task.details or {}
    locale = _task_locale(db, task.assigned_user_id)
    current_actor = _actor_label(db, task.assigned_user_id)
    payload = {
        "task_id": task.id,
        "task_type": task.task_type.value,
        "title": task.title,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "amount": details.get("amount") or details.get("total_outstanding"),
        "message": (
            notification_message
            if notification_message is not None
            else _notification_message(task, locale, current_actor=current_actor)
        ),
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    elif proactive:
        payload["reply_markup"] = _task_navigation_reply_markup(task, locale)
    generation = task.reminder_generation or 0
    return enqueue_notification(
        db,
        task_id=task.id,
        channel=NOTIFY_CHANNEL_TELEGRAM,
        recipient=recipient,
        payload=payload,
        dedupe_key=f"task:{task.id}:{generation}:{NOTIFY_CHANNEL_TELEGRAM}:{recipient}",
    )


def _register_task(
    db: Session,
    *,
    fields: dict,
    now: datetime,
    actor_id: int | None = None,
    notification_message: str | None = None,
    reply_markup: dict | None = None,
    refresh_on_conflict: bool = False,
    proactive: bool = False,
) -> tuple[OperationalTask | None, bool, bool]:
    """Create task + audit(task_created) + outbox in one transaction.

    Business-source tasks with no explicit assignee fall back to
    ``DEFAULT_ASSIGNED_USER_ID`` so proactive notifications get a recipient;
    recurring-rule tasks keep the rule's assignee as-is.

    When ``refresh_on_conflict`` is set and an ACTIVE task with the same
    ``dedupe_key`` already exists, the existing task is REFRESHED in place
    (task_reminded + new notification) instead of returning None — one
    business issue keeps exactly one active task while reminders stay current.

    ``proactive`` (default False) marks scheduler-generated reminders: the
    enqueue also claims the persistent daily dedupe slot, so the same business
    object + recipient + PH date + type is sent at most once per day even when
    a later scheduler pass refreshes the task (CONVERGENCE-003 §1.3/§1.4).

    Returns ``(task_or_None, notification_enqueued, is_new)`` where ``is_new``
    is False for a no-op or in-place refresh (the caller must not count it as
    a newly created task).
    """
    fields = dict(fields)
    if (
        fields.get("source_type") in BUSINESS_SOURCE_TYPES
        and fields.get("assigned_user_id") is None
    ):
        if DEFAULT_ASSIGNED_USER_ID is None:
            raise RuntimeError("OPERATIONS_DEFAULT_ASSIGNEE is not configured")
        fields["assigned_user_id"] = DEFAULT_ASSIGNED_USER_ID
    if refresh_on_conflict and fields.get("dedupe_key"):
        existing = _get_active_task_by_dedupe(db, fields["dedupe_key"])
        if existing is not None:
            refreshed, enqueued = _refresh_active_task(
                db,
                existing,
                fields=fields,
                now=now,
                actor_id=actor_id,
                notification_message=notification_message,
                reply_markup=reply_markup,
                proactive=proactive,
            )
            return refreshed, enqueued, False
    task = _insert_task_on_conflict_do_nothing(db, fields=fields)
    if task is None:
        return None, False, False
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action="task_created",
        actor_id=actor_id,  # None = system / scheduler
        new_value=serialize_row(task),
    )
    return task, _enqueue_for_task(
        db, task,
        notification_message=notification_message,
        reply_markup=reply_markup,
        proactive=proactive,
        now=now,
    ), True


def create_operational_task(
    db: Session,
    *,
    fields: dict,
    now: datetime | None = None,
    actor_id: int | None = None,
    notification_message: str | None = None,
    reply_markup: dict | None = None,
) -> tuple[OperationalTask | None, bool]:
    """Public seam for human-confirmed (V1.2.2 C2 copilot) task creation.

    Same atomic create + audit + outbox in ONE transaction as the scheduler's
    private ``_register_task`` — there is exactly ONE write path for
    ``operational_tasks``. ``actor_id`` records the human who confirmed the
    action (None = system/scheduler). Returns ``(task_or_None, enqueued)``;
    ``task_or_None`` is None when a PENDING task with the same ``dedupe_key``
    already exists (DB dedupe boundary = at-most-one active followup).
    ``notification_message`` is an OPT-IN outbox message override (English
    secretary card for copilot-confirmed followups); omitted keeps the
    scheduler's Chinese ``_notification_message``.
    """
    now = now or datetime.now(timezone.utc)
    task, enqueued, _is_new = _register_task(
        db,
        fields=fields,
        now=now,
        actor_id=actor_id,
        notification_message=notification_message,
        reply_markup=reply_markup,
    )
    return task, enqueued


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


def _complete_payment_task(db: Session, expense: Expense, *, now: datetime) -> None:
    """Complete the active PAYMENT_PENDING task for a now-fully-paid expense
    (derived truth §4/§13 / E12). One active task per dedupe_key is guaranteed
    by the DB partial index, so this finds a single candidate."""
    from app.services.operations.reconcile import auto_transition

    tasks = (
        db.query(OperationalTask)
        .filter(
            OperationalTask.source_type == "expense",
            OperationalTask.source_id == expense.id,
            OperationalTask.task_type == OperationalTaskType.PAYMENT_PENDING,
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    for task in tasks:
        auto_transition(
            db, task, to=OperationalTaskStatus.COMPLETED, now=now,
            reason="payment_fully_verified",
        )


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
            task, enqueued, is_new = _register_task(
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
                    # AI-OPS-FOUNDATION-001 §4: overdue rent is daily
                    # operational work -> SECRETARY, Owner only on escalation.
                    "assigned_user_id": secretary_assignee_id(),
                    "priority": OperationalTaskPriority.high,
                    "status": OperationalTaskStatus.PENDING,
                    "due_at": datetime.combine(oldest_due, time.min, tzinfo=now.tzinfo),
                    "dedupe_key": f"lease:{lease.id}:RENT_OVERDUE",
                    "details": _rent_task_details(
                        lease, unit, tenant,
                        {
                            "periods": [m for m, _ in overdue],
                            "total_outstanding": str(amount),
                            # TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §11: ``amount``
                            # must be the TOTAL arrears (monthly × uncovered
                            # periods), never a bare monthly rent. Previously
                            # ``_rent_task_details`` left ``amount=monthly_rent``
                            # and only ``total_outstanding`` carried the total,
                            # which made the Tasks board read a single month's
                            # rent instead of the sum.
                            "amount": str(amount),
                        },
                    ),
                },
                # AI-OPS-FOUNDATION-001 §2: a later pass that finds MORE overdue
                # periods refreshes the SAME logical task (updated periods/total,
                # fresh reminder) instead of creating a second active task.
                refresh_on_conflict=True,
                proactive=True,
            )
            if task is not None:
                created += 1 if is_new else 0
                notifications += 1 if enqueued else 0
                _supersede_rent_due(db, lease.id, now)

        for month, due in upcoming:
            # TELEGRAM-OPS-UX-CONVERGENCE-003 §1.2 (P0 root cause): a lease
            # that ALREADY has overdue periods must NOT create RENT_DUE tasks
            # for upcoming periods. The RENT_OVERDUE task supersedes every
            # PENDING RENT_DUE of the lease in the same pass
            # (``_supersede_rent_due``), so re-creating RENT_DUE produced a
            # create -> supersede -> create loop that enqueued and sent ONE
            # reminder per worker pass (the observed per-minute spam).
            if overdue:
                continue
            task, enqueued, is_new = _register_task(
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
                    "assigned_user_id": secretary_assignee_id(),
                    "priority": OperationalTaskPriority.medium,
                    "status": OperationalTaskStatus.PENDING,
                    "due_at": datetime.combine(due, time.min, tzinfo=now.tzinfo),
                    "dedupe_key": f"lease:{lease.id}:RENT_DUE:{month}",
                    "details": _rent_task_details(lease, unit, tenant, {"period": month}),
                },
                refresh_on_conflict=True,
                proactive=True,
            )
            if task is not None:
                created += 1 if is_new else 0
                notifications += 1 if enqueued else 0

    # --- LEASE_EXPIRING ----------------------------------------------------------
    expiry_window_end = today + timedelta(days=LEASE_EXPIRY_WINDOW_DAYS)
    for lease in leases:
        if not (today <= lease.end_date <= expiry_window_end):
            continue
        unit = units.get(lease.unit_id)
        task, enqueued, is_new = _register_task(
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
                # Routine operational follow-up -> Secretary (not Owner).
                "assigned_user_id": secretary_assignee_id(),
                "priority": OperationalTaskPriority.medium,
                "status": OperationalTaskStatus.PENDING,
                "due_at": datetime.combine(lease.end_date, time.min, tzinfo=now.tzinfo),
                "dedupe_key": f"lease:{lease.id}:LEASE_EXPIRING",
                "details": {"lease_id": lease.id, "end_date": lease.end_date.isoformat()},
            },
            proactive=True,
        )
        if task is not None:
            created += 1 if is_new else 0
            notifications += 1 if enqueued else 0

    # --- APPROVAL_PENDING / PAYMENT_PENDING (expenses, read-only) ----------------
    approval_cutoff = now - timedelta(days=APPROVAL_PENDING_AFTER_DAYS)
    pending_expenses = (
        db.query(Expense)
        .filter(Expense.status == ExpenseStatus.pending, Expense.created_at <= approval_cutoff)
        .all()
    )
    for expense in pending_expenses:
        task, enqueued, is_new = _register_task(
            db,
            now=now,
            fields={
                "task_type": OperationalTaskType.APPROVAL_PENDING,
                "title": _expense_task_title("待批准支出", expense),
                "source_type": "expense",
                "source_id": expense.id,
                # AI-OPS-FOUNDATION-001 §4/§7: approval is Owner-only (the
                # approver), never routine Secretary work.
                "assigned_user_id": DEFAULT_ASSIGNED_USER_ID,
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
                    "payer_user_id": expense.payer_user_id,
                },
            },
            reply_markup=_expense_reply_markup(
                expense.id, has_receipt=bool(expense.receipt_attachment_id)
            ),
            proactive=True,
        )
        if task is not None:
            created += 1 if is_new else 0
            notifications += 1 if enqueued else 0

    payment_cutoff = now - timedelta(days=PAYMENT_PENDING_AFTER_DAYS)
    # PASAY-EXPENSE-OPERATION-003B: a PAYMENT_PENDING task exists only while
    # there is still REAL remaining money to pay (derived from VERIFIED claims,
    # §4). Fully-paid expenses are skipped; partial expenses refresh the task
    # amount to the REMAINING balance. The dedupe boundary guarantees at most
    # one active payment task per expense (§13 / E11).
    payable_expenses = (
        db.query(Expense)
        .filter(
            Expense.status.in_([
                ExpenseStatus.approved,
                ExpenseStatus.partially_paid,
                ExpenseStatus.payment_claimed,
            ]),
            (Expense.approved_at.isnot(None)) & (Expense.approved_at <= payment_cutoff),
        )
        .all()
    )
    from app.services.expense_payment_truth import payment_truth

    for expense in payable_expenses:
        truth = payment_truth(db, expense)
        if truth.fully_paid or truth.remaining <= 0:
            # Fully verified -> the payment task must be completed (not re-created).
            _complete_payment_task(db, expense, now=now)
            continue
        # AI-OPS-FOUNDATION-001 §4/§8: the next action belongs to the ACTUAL
        # payer (expense.payer_user_id); Owner is only the fallback when the
        # payer was never recorded.
        payer = expense.payer_user_id or DEFAULT_ASSIGNED_USER_ID
        title = _expense_task_title("待付款支出", expense)
        if truth.pending_claims > 0:
            title = _expense_task_title("待核验付款", expense)
        task, enqueued, is_new = _register_task(
            db,
            now=now,
            fields={
                "task_type": OperationalTaskType.PAYMENT_PENDING,
                "title": title,
                "source_type": "expense",
                "source_id": expense.id,
                "assigned_user_id": payer,
                "priority": OperationalTaskPriority.high,
                "status": OperationalTaskStatus.PENDING,
                "due_at": expense.due_date and datetime.combine(expense.due_date, time.min, tzinfo=now.tzinfo)
                or expense.approved_at or now,
                "dedupe_key": f"expense:{expense.id}:PAYMENT_PENDING",
                "details": {
                    "expense_id": expense.id,
                    "amount": str(truth.remaining),
                    "remaining": str(truth.remaining),
                    "verified_paid": str(truth.verified_paid),
                    "category": expense.category,
                    "payee": expense.payee,
                    "payer_user_id": payer,
                    "pending_claims": truth.pending_claims,
                    "fully_paid": truth.fully_paid,
                },
            },
            reply_markup=_expense_reply_markup(
                expense.id, has_receipt=bool(expense.receipt_attachment_id)
            ),
            proactive=True,
        )
        if task is not None:
            created += 1 if is_new else 0
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
        task, enqueued, is_new = _register_task(
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
            proactive=True,
        )
        if task is not None:
            created += 1 if is_new else 0
            notifications += 1 if enqueued else 0

    return created, notifications


def generate_rule_task(db: Session, rule, *, now: datetime) -> tuple[OperationalTask | None, bool]:
    """Generate one task for a claimed recurring rule and advance its
    next_run_at. Returns (task_or_None, notification_enqueued)."""
    period_key = period_key_for(rule, now)
    details = dict(rule.details or {})
    details["rule_id"] = rule.id
    details["period"] = period_key
    task, enqueued, _is_new = _register_task(
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
        proactive=True,
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
