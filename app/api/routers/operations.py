"""V1.2 PROACTIVE OPERATIONS API router (/api/v1/operations).

RBAC (re-checked on EVERY request, including task transitions):
- OWNER: everything, with explicit org_id scope
- SECRETARY: view + process operational tasks, manage recurring rules, with org_id scope
- SECRETARY self-filter: only view / process tasks assigned to themselves (403 otherwise) when user is not OWNER

Task handlers NEVER write incomes/expenses/commission settlements — the
V1.1 financial state machine is the only writer for those tables.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.api.deps import (
    SystemReader,
    get_current_user,
    get_operations_reader,
)
from app.config import settings
from app.database import get_db
from app.models.copilot import CopilotActionProposal, CopilotActionStatus
from app.models.operations import (
    NotificationOutbox,
    NotificationStatus,
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
    RecurringRule,
)
from app.models.financial import Expense
from app.models.lease import Lease
from app.models.membership import Membership, MembershipState, OrganizationRole
from app.models.property import Property, Unit
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.organization_scope import list_active_org_ids_for_user
from app.schemas.operations import (
    OperationsSummary,
    OperationalTaskRead,
    PaymentPromiseIn,
    PaymentPromiseOut,
    RecurringRuleCreate,
    RecurringRuleRead,
    RecurringRuleUpdate,
    ResumeActionIn,
    ResumeActionOut,
    SchedulerRunResult,
    TaskActionOut,
    TaskCreateIn,
    TaskFollowupDeliveryIn,
    TaskFollowupDeliveryOut,
    TaskSnoozeIn,
    TaskUpdateIn,
)
from app.schemas.copilot import (
    CopilotAskIn,
    CopilotAskOut,
    CopilotExecuteOut,
    CopilotNlParseIn,
    CopilotNlParseOut,
    CopilotProposalActionOut,
    CopilotProposalCard,
    CopilotProposalCreate,
    CopilotProposalRead,
    CopilotRecommendIn,
    CopilotRecommendOut,
    CopilotTodayIn,
    CopilotTodayOut,
    CopilotWhyIn,
    CopilotWhyOut,
    LatencyOut,
)
from app.services.audit import record_audit, serialize_row
from app.services.copilot import ask as copilot_ask_svc
from app.services.copilot import llm as copilot_llm
from app.services.copilot import nl_parse as copilot_nl_parse_svc
from app.services.copilot import today as copilot_today_svc
from app.services.copilot import today_fast as copilot_today_fast_svc
from app.services.copilot import why as copilot_why_svc
from app.services.copilot import execute as copilot_execute
from app.services.operations import copilot as copilot_svc
from app.services.operations import proposals as copilot_proposals
from app.services.operations import quick as quick_svc
from app.services.operations.config import NOTIFY_CHANNEL_TELEGRAM, NOTIFY_MAX_ATTEMPTS
from app.services.operations.notifier import (
    TelegramSender,
    _claim_row,
    _finalize_failed,
    _finalize_sent,
    _message_text,
)
from app.services.operations.outbox import enqueue_notification, resolve_recipient
from app.services.operations.redelivery import suppress_pending_redeliveries
from app.services.operations.scheduler import run_scheduler_once
from app.services.operations.generation import create_operational_task
from app.services.operations.owner_scope import is_owner_actionable
from app.services.operations.truth_validator import assert_completion_allowed

router = APIRouter(prefix="/operations", tags=["operations"])

SNOOZE_PRESETS = {"1h", "today_afternoon", "tomorrow_morning", "3d"}


def resolve_org_membership(
    role: Iterable[OrganizationRole] | None = None,
):
    """Depends factory: resolve ACTIVE Membership for the current user+org scope.

    Returns Membership object (with .organization_id). Raises HTTP 403 on role
    mismatch / missing membership, or HTTP 409 when a SYSTEM reader presents
    an ambiguous org scope (multiple ACTIVE memberships, no explicit trusted
    org context).

    HUMAN path (unchanged contract):
      * Uses the HUMAN user's ACTIVE memberships via list_active_org_ids_for_user.
      * No ACTIVE membership -> HTTP 403.
      * Otherwise picks the caller's first active org_id (HUMANs are expected to
        belong to a single active org in the frozen topology).
      * Role mismatch on the resolved membership -> HTTP 403.

    SYSTEM / SystemReader path (fail-closed for ambiguous scope):
      * A SYSTEM reader must never implicitly fall back to the "first available"
        ACTIVE membership (no .first() / no ordered select / no memberships[0]).
      * Decisions (after filtering state=ACTIVE + removed_at=NULL + role match):
        * 0 qualifying memberships  -> HTTP 403 "Active organization membership
          required" (fail-closed, no org exposure).
        * exactly 1 qualifying membership -> use that unique Membership.  The
          singleton case is unambiguous even without an explicit trusted org
          context, so legacy single-org deployments keep working.
        * >= 2 qualifying memberships AND a trusted organization context is
          available -> query ONLY for that single organization_id and confirm
          an ACTIVE, role-matching Membership exists.  No fallback to other
          memberships on mismatch; fail 403 immediately.  The trusted org
          context currently comes from the request's reader-bound scheduler
          operation (a SystemReader operation always has a single-org target
          already proven by the caller), mirrored through the resolved reader.
        * >= 2 qualifying memberships WITHOUT a trusted explicit organization
          context -> HTTP 409 "Organization context required" (fail-closed).
          No DB default ordering / no "pick first" heuristic — ambiguous scope
          is rejected explicitly.
    """
    role_set: set[OrganizationRole] = set(role) if role else set()

    def _base_membership_query(db: Session):
        q = db.query(Membership).filter(
            Membership.state == MembershipState.ACTIVE,
            Membership.removed_at.is_(None),
        )
        if role_set:
            q = q.filter(Membership.role.in_(role_set))
        return q

    def _dep(
        db: Session = Depends(get_db),
        reader: User | SystemReader = Depends(get_operations_reader),
    ) -> Membership:
        if isinstance(reader, SystemReader):
            # Explicit trusted organization_id context for the SYSTEM reader is
            # carried on the reader.credential when an internal caller binds it
            # to a single target org.  SystemReader uses __slots__ so the
            # credential SQLAlchemy instance (with __dict__) is used as the
            # stable attachment surface for internal scope hints.  When absent
            # (legacy single-org jobs), attribute is None and we fall to the
            # 0/1/N ambiguity guard below.
            trusted_org_id: int | None = getattr(
                reader.credential, "trusted_organization_id", None
            )
            base_q = _base_membership_query(db)

            if trusted_org_id is not None:
                # Case: explicit trusted org context.  Only the trusted org
                # qualifies; never fall back to other memberships even if this
                # one is missing.
                picked = (
                    base_q.filter(Membership.organization_id == trusted_org_id)
                    .one_or_none()
                )
                if picked is None:
                    raise HTTPException(
                        status_code=403,
                        detail="Active organization membership required",
                    )
                return picked

            # No explicit trusted org context.  Ambiguity guard: read up to 2
            # rows with an UNORDERED query so DB default ordering can never
            # choose an org for us.  Exactly 1 is unambiguous; 0 and >=2 are
            # fail-closed with stable contracts above.
            small_batch = base_q.limit(2).all()
            if len(small_batch) == 0:
                raise HTTPException(
                    status_code=403,
                    detail="Active organization membership required",
                )
            if len(small_batch) == 1:
                return small_batch[0]
            raise HTTPException(
                status_code=409,
                detail="Organization context required",
            )

        user = reader
        org_ids = list_active_org_ids_for_user(db, user.id)
        if not org_ids:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No active organization membership")
        org_id = org_ids[0]
        q = (
            db.query(Membership)
            .filter(
                Membership.user_id == user.id,
                Membership.organization_id == org_id,
                Membership.state == MembershipState.ACTIVE,
                Membership.removed_at.is_(None),
            )
        )
        if role_set:
            q = q.filter(Membership.role.in_(role_set))
        membership = q.first()
        if membership is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient organization role")
        return membership

    return _dep


def _org_property_ids(db: Session, org_id: int) -> set[int]:
    rows = db.execute(
        select(Property.id).where(
            Property.organization_id == org_id,
            Property.deleted_at.is_(None),
        )
    ).all()
    return {r[0] for r in rows}


def _org_lease_ids(db: Session, org_id: int) -> set[int]:
    rows = db.execute(
        select(Lease.id)
        .join(Unit, Unit.id == Lease.unit_id)
        .join(Property, Property.id == Unit.property_id)
        .where(
            Property.organization_id == org_id,
            Lease.deleted_at.is_(None),
        )
    ).all()
    return {r[0] for r in rows}


def _org_tenant_ids(db: Session, org_id: int) -> set[int]:
    rows = db.execute(
        select(Tenant.id).where(
            Tenant.organization_id == org_id,
            Tenant.deleted_at.is_(None),
        )
    ).all()
    return {r[0] for r in rows}


def _scoped_task_query(db: Session, org_id: int):
    """Return a WHERE clause fragment for scoping OperationalTask to org_id.

    Three-channel OR: any of property_id / lease_id / tenant_id links the
    task to the organization.
    """
    pids = _org_property_ids(db, org_id)
    lids = _org_lease_ids(db, org_id)
    tids = _org_tenant_ids(db, org_id)
    if not pids and not lids and not tids:
        return OperationalTask.id == -1
    or_terms = []
    if pids:
        or_terms.append(OperationalTask.property_id.in_(list(pids)))
    if lids:
        or_terms.append(OperationalTask.lease_id.in_(list(lids)))
    if tids:
        or_terms.append(OperationalTask.tenant_id.in_(list(tids)))
    return or_(*or_terms)


def _scoped_get_task(db: Session, task_id: int, org_id: int) -> OperationalTask:
    """scoped_get for OperationalTask: LookupError semantics → HTTP 404 if cross-org or missing."""
    where_scope = _scoped_task_query(db, org_id)
    task = (
        db.query(OperationalTask)
        .filter(OperationalTask.id == task_id, where_scope)
        .first()
    )
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Operational task not found")
    return task


def _scoped_lock_task_for_update(db: Session, task_id: int, org_id: int) -> OperationalTask:
    where_scope = _scoped_task_query(db, org_id)
    task = (
        db.execute(
            select(OperationalTask)
            .where(OperationalTask.id == task_id, where_scope)
            .with_for_update()
        )
        .scalar_one_or_none()
    )
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Operational task not found")
    return task


def _self_filter_required(user: User, membership: Membership) -> bool:
    """True if the caller should only see tasks assigned to themselves.

    OWNER and SECRETARY-manager tiers see every task in the org (bypass).
    Only SECRETARY-agent tier (UserRole.agent inside a SECRETARY org
    membership) is restricted to their own assigned tasks (double-checked
    by :func:`_agent_scope` as a defense-in-depth redundancy).
    """
    return user.role == UserRole.agent


def _forbid_agent_role(user: User) -> None:
    """UserRole.agent is a worker tier within SECRETARY org membership: it
    can act on tasks but never CRUD rules, approve expenses or configure the
    business. Always checked after org-level SECRETARY/OWNER membership gate.
    """
    if user.role == UserRole.agent:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Agent role cannot manage rules, approval or financial configuration",
        )


def _agent_scope(query, user: User):
    """Agents only ever see their own assigned tasks."""
    if user.role == UserRole.agent:
        return query.filter(OperationalTask.assigned_user_id == user.id)
    return query


def _get_task_or_404(db: Session, task_id: int) -> OperationalTask:
    task = db.query(OperationalTask).filter(OperationalTask.id == task_id).first()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Operational task not found")
    return task


def _require_access(task: OperationalTask, user: User) -> None:
    if user.role == UserRole.agent and task.assigned_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a task assigned to another user"
        )


def _task_property_code(db: Session, task: OperationalTask) -> str | None:
    """Derived display unit code for a task ("1680", or "BAY-1680" only when
    the unit number repeats across properties). Never an internal UUID."""
    if task.lease_id is not None:
        from app.models.lease import Lease
        from app.models.property import Unit
        lease = db.query(Lease).filter(Lease.id == task.lease_id).first()
        if lease is not None:
            unit = db.query(Unit).filter(Unit.id == lease.unit_id).first()
            if unit is not None:
                dupes = (
                    db.query(Unit.id)
                    .filter(
                        Unit.unit_number == unit.unit_number,
                        Unit.deleted_at.is_(None),
                        Unit.is_active.is_(True),
                    )
                    .count()
                )
                if dupes <= 1:
                    return unit.unit_number
                from app.models.property import Property
                prop = db.query(Property).filter(Property.id == unit.property_id).first()
                prefix = (prop.name or str(unit.property_id)).split()[0][:4].upper() if prop else str(unit.property_id)
                return f"{prefix}-{unit.unit_number}"
    details = task.details or {}
    if details.get("unit_number"):
        return str(details["unit_number"])
    return None


def _task_read(db: Session, task: OperationalTask) -> OperationalTaskRead:
    """Serialize one task with the derived property_code for the V2 cards."""
    data = serialize_row(task)
    if "metadata" in data and "details" not in data:
        data["details"] = data.pop("metadata")
    data["property_code"] = _task_property_code(db, task)
    return OperationalTaskRead(**data)


def _lock_task_for_update(db: Session, task_id: int) -> OperationalTask:
    task = (
        db.execute(
            select(OperationalTask)
            .where(OperationalTask.id == task_id)
            .with_for_update()
        )
        .scalar_one_or_none()
    )
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Operational task not found")
    return task


def _rent_followup_dedupe_key(task_id: int, recipient: str) -> str:
    return f"rent-followup:{task_id}:{NOTIFY_CHANNEL_TELEGRAM}:{recipient}"


def _build_notification_sender(db: Session) -> TelegramSender:
    def resolve_user(user_id_str: str) -> str | None:
        from app.services.identity import resolve_telegram_destination

        return resolve_telegram_destination(db, int(user_id_str))

    return TelegramSender(settings.telegram_bot_token, resolve_user=resolve_user)


def _load_outbox_by_dedupe(db: Session, dedupe_key: str) -> NotificationOutbox | None:
    return (
        db.query(NotificationOutbox)
        .filter(NotificationOutbox.dedupe_key == dedupe_key)
        .first()
    )


def _sync_rent_followup_assignment(
    db: Session,
    *,
    task_id: int,
    assignee_user_id: int,
    actor_id: int,
    now: datetime,
) -> OperationalTask:
    task = _get_task_or_404(db, task_id)
    details = dict(task.details or {})
    old = serialize_row(task)
    changed = False
    if task.assigned_user_id != assignee_user_id:
        task.assigned_user_id = assignee_user_id
        changed = True
    if details.get("assigned_to") != assignee_user_id:
        details["assigned_to"] = assignee_user_id
        changed = True
    if not details.get("assigned_at"):
        details["assigned_at"] = now.isoformat()
        changed = True
    if task.next_action != "Secretary to contact tenant for overdue rent.":
        task.next_action = "Secretary to contact tenant for overdue rent."
        changed = True
    if not changed:
        return task
    task.details = details
    task.updated_by = actor_id
    task.updated_at = now
    task.reminder_generation = (task.reminder_generation or 0) + 1
    db.flush()
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action="task_updated",
        actor_id=actor_id,
        old_value=old,
        new_value=serialize_row(task),
    )
    db.commit()
    db.refresh(task)
    return task


def _deliver_rent_followup_outbox_row(
    db: Session,
    *,
    outbox_id: int,
    now: datetime,
) -> tuple[str, NotificationOutbox | None]:
    current = db.get(NotificationOutbox, outbox_id)
    if current is None:
        return "missing", None
    if current.status == NotificationStatus.SENT:
        return "sent", current
    if current.status == NotificationStatus.FAILED:
        db.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.id == outbox_id,
                NotificationOutbox.status == NotificationStatus.FAILED,
            )
            .values(
                status=NotificationStatus.PENDING,
                next_attempt_at=None,
                claimed_at=None,
                updated_at=now,
            ),
            execution_options={"synchronize_session": False},
        )
        db.commit()
    row = _claim_row(db, outbox_id, now=now)
    if row is None:
        current = db.get(NotificationOutbox, outbox_id)
        if current is not None and current.status == NotificationStatus.SENT:
            return "sent", current
        return "processing", current
    sender = _build_notification_sender(db)
    try:
        message_id = sender.send(
            row.recipient,
            _message_text(row),
            reply_markup=row.payload.get("reply_markup") if row.payload else None,
        )
        if _finalize_sent(db, row, message_id=message_id, now=now):
            return "sent", db.get(NotificationOutbox, outbox_id)
        current = db.get(NotificationOutbox, outbox_id)
        if current is not None and current.status == NotificationStatus.SENT:
            return "sent", current
        return "processing", current
    except Exception as exc:  # noqa: BLE001 - surfaced as retryable delivery failure
        _finalize_failed(
            db,
            row,
            exc,
            now=now,
            max_attempts=NOTIFY_MAX_ATTEMPTS,
            backoff_base=0,
        )
        return "failed", db.get(NotificationOutbox, outbox_id)


def _check_assignee(db: Session, user_id: int | None) -> None:
    if user_id is None:
        return
    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assigned user not found")


def _check_property(db: Session, property_id: int | None) -> None:
    if property_id is None:
        return
    from app.models.property import Property
    prop = db.query(Property).filter(Property.id == property_id).first()
    if prop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")


def _resolve_snooze_until(payload: TaskSnoozeIn, now: datetime) -> datetime:
    if payload.until is not None:
        if payload.until <= now:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "Snooze until must be in the future"
            )
        return payload.until
    if payload.preset is None or payload.preset not in SNOOZE_PRESETS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"preset must be one of {sorted(SNOOZE_PRESETS)} or provide until",
        )
    if payload.preset == "1h":
        return now + timedelta(hours=1)
    if payload.preset == "today_afternoon":
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    if payload.preset == "tomorrow_morning":
        target = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        return target
    return now + timedelta(days=3)  # "3d"


def _replay_or_conflict(db: Session, task_id: int, want_status, conflict_detail: str):
    current = _get_task_or_404(db, task_id)
    if current.status == want_status:
        return current
    raise HTTPException(status.HTTP_409_CONFLICT, conflict_detail)


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

@router.get("/tasks", response_model=list[OperationalTaskRead])
def list_tasks(
    property_id: int | None = Query(default=None),
    assignee: int | None = Query(default=None),
    task_type: OperationalTaskType | None = Query(default=None),
    status: OperationalTaskStatus | None = Query(default=None),
    scope: str | None = Query(default=None, description="'owner' = Owner attention filter"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    query = db.query(OperationalTask).filter(_scoped_task_query(db, org_id))
    if _self_filter_required(user, membership):
        query = query.filter(OperationalTask.assigned_user_id == user.id)
    if membership.role == OrganizationRole.OWNER and assignee is not None:
        query = query.filter(OperationalTask.assigned_user_id == assignee)
    if property_id is not None:
        if property_id not in _org_property_ids(db, org_id):
            return []
        query = query.filter(OperationalTask.property_id == property_id)
    if task_type is not None:
        query = query.filter(OperationalTask.task_type == task_type)
    if status is not None:
        query = query.filter(OperationalTask.status == status)
    rows = query.order_by(OperationalTask.due_at, OperationalTask.id).all()
    if scope == "owner":
        rows = [t for t in rows if is_owner_actionable(t, user)]
    return rows


@router.get("/tasks/{task_id}", response_model=OperationalTaskRead)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    task = _scoped_get_task(db, task_id, org_id)
    if _self_filter_required(user, membership) and task.assigned_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a task assigned to another user"
        )
    return task


@router.post("/tasks/{task_id}/complete", response_model=TaskActionOut)
def complete_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    task = _scoped_get_task(db, task_id, org_id)
    if _self_filter_required(user, membership) and task.assigned_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a task assigned to another user"
        )
    now = datetime.now(timezone.utc)
    if task.status == OperationalTaskStatus.PENDING:
        assert_completion_allowed(db, task)
    old = serialize_row(task)
    result = db.execute(
        update(OperationalTask)
        .where(OperationalTask.id == task_id, OperationalTask.status == OperationalTaskStatus.PENDING)
        .values(
            status=OperationalTaskStatus.COMPLETED,
            completed_at=now,
            completed_by=user.id,
            reminder_generation=OperationalTask.reminder_generation + 1,
            updated_by=user.id,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(task)
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_completed",
            actor_id=user.id,
            changed_fields={
                "status": ["PENDING", "COMPLETED"],
                "reminder_generation": [old.get("reminder_generation", 0), task.reminder_generation],
            },
            old_value=old,
            new_value=serialize_row(task),
        )
        suppress_pending_redeliveries(
            db, task_id, actor_id=user.id, reason="task_completed", now=now
        )
        if task.task_type == OperationalTaskType.AC_MAINTENANCE:
            from app.services.operations.repair_flow import ensure_evidence_followup

            ensure_evidence_followup(db, task, now=now, actor_id=user.id)
        db.commit()
        db.refresh(task)
        return TaskActionOut(task=task, detail="Task completed")
    db.rollback()
    current = _replay_or_conflict(
        db, task_id, OperationalTaskStatus.COMPLETED, "Cannot complete a cancelled task"
    )
    return TaskActionOut(task=current, detail="Task already completed")


@router.post("/tasks/{task_id}/snooze", response_model=TaskActionOut)
def snooze_task(
    task_id: int,
    payload: TaskSnoozeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    task = _scoped_get_task(db, task_id, org_id)
    if _self_filter_required(user, membership) and task.assigned_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a task assigned to another user"
        )
    now = datetime.now(timezone.utc)
    until = _resolve_snooze_until(payload, now)
    old = serialize_row(task)
    result = db.execute(
        update(OperationalTask)
        .where(OperationalTask.id == task_id, OperationalTask.status == OperationalTaskStatus.PENDING)
        .values(
            snoozed_until=until,
            reminder_generation=OperationalTask.reminder_generation + 1,
            updated_by=user.id,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(task)
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_snoozed",
            actor_id=user.id,
            changed_fields={
                "snoozed_until": [None, until.isoformat()],
                "reminder_generation": [old.get("reminder_generation", 0), task.reminder_generation],
            },
            old_value=old,
            new_value=serialize_row(task),
        )
        suppress_pending_redeliveries(
            db, task_id, actor_id=user.id, reason="task_snoozed", now=now
        )
        db.commit()
        db.refresh(task)
        return TaskActionOut(task=task, detail="Task snoozed")
    db.rollback()
    current = _get_task_or_404(db, task_id)
    if current.status != OperationalTaskStatus.PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only pending tasks can be snoozed")
    if current.snoozed_until == until:
        return TaskActionOut(task=current, detail="Task already snoozed")
    raise HTTPException(status.HTTP_409_CONFLICT, "Task was changed; refresh and retry")


@router.post("/tasks/{task_id}/cancel", response_model=TaskActionOut)
def cancel_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    task = _scoped_get_task(db, task_id, org_id)
    if _self_filter_required(user, membership) and task.assigned_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a task assigned to another user"
        )
    now = datetime.now(timezone.utc)
    old = serialize_row(task)
    result = db.execute(
        update(OperationalTask)
        .where(OperationalTask.id == task_id, OperationalTask.status == OperationalTaskStatus.PENDING)
        .values(
            status=OperationalTaskStatus.CANCELLED,
            reminder_generation=OperationalTask.reminder_generation + 1,
            updated_by=user.id,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(task)
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_cancelled",
            actor_id=user.id,
            changed_fields={
                "status": ["PENDING", "CANCELLED"],
                "reminder_generation": [old.get("reminder_generation", 0), task.reminder_generation],
            },
            old_value=old,
            new_value=serialize_row(task),
        )
        suppress_pending_redeliveries(
            db, task_id, actor_id=user.id, reason="task_cancelled", now=now
        )
        db.commit()
        db.refresh(task)
        return TaskActionOut(task=task, detail="Task cancelled")
    db.rollback()
    current = _replay_or_conflict(
        db, task_id, OperationalTaskStatus.CANCELLED, "Cannot cancel a completed task"
    )
    return TaskActionOut(task=current, detail="Task already cancelled")


@router.post("/tasks/{task_id}/acknowledge", response_model=TaskActionOut)
def acknowledge_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    task = _scoped_get_task(db, task_id, org_id)
    if _self_filter_required(user, membership) and task.assigned_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a task assigned to another user"
        )
    now = datetime.now(timezone.utc)
    if task.status != OperationalTaskStatus.PENDING:
        return TaskActionOut(task=task, detail="Task already acknowledged")
    next_action = task.next_action or f"Acknowledged: {task.title}"
    next_check_at = task.next_check_at or (now + timedelta(days=1))
    old = serialize_row(task)
    result = db.execute(
        update(OperationalTask)
        .where(OperationalTask.id == task_id, OperationalTask.status == OperationalTaskStatus.PENDING)
        .values(
            status=OperationalTaskStatus.IN_PROGRESS,
            next_action=next_action,
            next_check_at=next_check_at,
            reminder_generation=OperationalTask.reminder_generation + 1,
            updated_by=user.id,
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount == 1:
        db.refresh(task)
        record_audit(
            db,
            table_name="operational_tasks",
            record_id=task.id,
            action="task_acknowledged",
            actor_id=user.id,
            changed_fields={
                "status": ["PENDING", "IN_PROGRESS"],
                "reminder_generation": [old.get("reminder_generation", 0), task.reminder_generation],
            },
            old_value=old,
            new_value=serialize_row(task),
        )
        suppress_pending_redeliveries(
            db, task_id, actor_id=user.id, reason="task_acknowledged", now=now
        )
        db.commit()
        db.refresh(task)
        return TaskActionOut(task=task, detail="Task acknowledged")
    db.rollback()
    current = _get_task_or_404(db, task_id)
    return TaskActionOut(task=current, detail="Task already acknowledged")


# ---------------------------------------------------------------------------
# PASAY-V2-FOUNDATION-001: conversation-driven task updates / creation
# ---------------------------------------------------------------------------

def _validate_transition(
    task: OperationalTask,
    want_status: OperationalTaskStatus,
    payload: TaskUpdateIn,
) -> None:
    """V2 transition rules:
    - PENDING -> IN_PROGRESS requires next_action AND next_check_at.
    - IN_PROGRESS -> COMPLETED is the only forward path (completed_at set).
    - CANCELLED is terminal.
    """
    if task.status == OperationalTaskStatus.CANCELLED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot update a cancelled task")
    if want_status == task.status:
        return
    if task.status == OperationalTaskStatus.PENDING and want_status == OperationalTaskStatus.IN_PROGRESS:
        next_action = payload.next_action if payload.next_action is not None else task.next_action
        next_check_at = payload.next_check_at if payload.next_check_at is not None else task.next_check_at
        if not next_action or not next_check_at:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "IN_PROGRESS requires next_action and next_check_at",
            )
        return
    if task.status == OperationalTaskStatus.IN_PROGRESS and want_status == OperationalTaskStatus.COMPLETED:
        return
    if want_status in (
        OperationalTaskStatus.PENDING,
        OperationalTaskStatus.IN_PROGRESS,
        OperationalTaskStatus.COMPLETED,
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Invalid transition {task.status.value} -> {want_status.value}",
        )


@router.patch("/tasks/{task_id}", response_model=TaskActionOut)
def update_task(
    task_id: int,
    payload: TaskUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    task = _scoped_get_task(db, task_id, org_id)
    if _self_filter_required(user, membership) and task.assigned_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a task assigned to another user"
        )
    now = datetime.now(timezone.utc)
    want_status = payload.status if payload.status is not None else task.status
    _validate_transition(task, want_status, payload)
    old = serialize_row(task)

    updates: dict = {}
    if payload.title is not None:
        updates["title"] = payload.title
    if payload.due_at is not None:
        updates["due_at"] = payload.due_at
    if payload.next_action is not None:
        updates["next_action"] = payload.next_action
    if payload.next_check_at is not None:
        updates["next_check_at"] = payload.next_check_at
    if payload.context is not None:
        updates["context"] = payload.context
    if payload.completion_condition is not None:
        updates["completion_condition"] = payload.completion_condition
    if payload.details is not None:
        # AI-OPS-FOUNDATION-001 §8: structured details (promise/escalation)
        # are MERGED into the JSONB so a follow-up update never wipes other
        # task metadata.
        merged_details = dict(task.details or {})
        for key, value in payload.details.items():
            if isinstance(value, dict) and isinstance(merged_details.get(key), dict):
                merged_details[key] = {**merged_details[key], **value}
            else:
                merged_details[key] = value
        updates["details"] = merged_details
    if payload.status is not None:
        updates["status"] = payload.status
        if payload.status == OperationalTaskStatus.COMPLETED and task.status != OperationalTaskStatus.COMPLETED:
            assert_completion_allowed(db, task)
            updates["completed_at"] = now
            updates["completed_by"] = user.id
    if not updates:
        return TaskActionOut(task=_task_read(db, task), detail="No changes")

    updates["updated_by"] = user.id
    updates["updated_at"] = now
    updates["reminder_generation"] = (task.reminder_generation or 0) + 1
    for key, value in updates.items():
        setattr(task, key, value)
    db.flush()
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action="task_updated",
        actor_id=user.id,
        old_value=old,
        new_value=serialize_row(task),
    )
    if task.status == OperationalTaskStatus.COMPLETED:
        suppress_pending_redeliveries(db, task.id, actor_id=user.id, reason="task_completed", now=now)
        # AI-OPS-FOUNDATION-001 §13: a repair that completes without minimal
        # completion evidence gets a SECRETARY follow-up (never the Owner).
        if task.task_type == OperationalTaskType.AC_MAINTENANCE:
            from app.services.operations.repair_flow import ensure_evidence_followup

            ensure_evidence_followup(db, task, now=now, actor_id=user.id)
    db.commit()
    db.refresh(task)
    detail = "Task completed" if task.status == OperationalTaskStatus.COMPLETED else "Task updated"
    return TaskActionOut(task=_task_read(db, task), detail=detail)


@router.post("/tasks/{task_id}/followup-delivery", response_model=TaskFollowupDeliveryOut)
def deliver_task_followup(
    task_id: int,
    payload: TaskFollowupDeliveryIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Deliver one rent follow-up DM via the canonical outbox/notifier seam."""
    org_id = membership.organization_id
    now = datetime.now(timezone.utc)
    task = _scoped_lock_task_for_update(db, task_id, org_id)
    if _self_filter_required(user, membership) and task.assigned_user_id != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Cannot access a task assigned to another user"
        )
    if task.status != OperationalTaskStatus.PENDING:
        db.commit()
        return TaskFollowupDeliveryOut(
            task=_task_read(db, task),
            delivery_state="TASK_NOT_PENDING",
            detail="Task is no longer pending",
            telegram_message_id=None,
        )
    details = dict(task.details or {})
    if details.get("assigned_to"):
        db.commit()
        return TaskFollowupDeliveryOut(
            task=_task_read(db, task),
            delivery_state="ALREADY_DELIVERED",
            detail="Follow-up already assigned",
            telegram_message_id=None,
        )
    _check_assignee(db, payload.assignee_user_id)
    recipient = resolve_recipient(db, payload.assignee_user_id)
    if not recipient:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Assignee has no Telegram destination")
    dedupe_key = _rent_followup_dedupe_key(task.id, recipient)
    outbox = _load_outbox_by_dedupe(db, dedupe_key)
    followup_payload = {
        "task_id": task.id,
        "task_type": task.task_type.value,
        "title": task.title,
        "due_at": task.due_at.isoformat(),
        "message": payload.message,
        "reply_markup": payload.reply_markup,
    }
    if outbox is None:
        enqueue_notification(
            db,
            task_id=task.id,
            channel=NOTIFY_CHANNEL_TELEGRAM,
            recipient=recipient,
            payload=followup_payload,
            dedupe_key=dedupe_key,
        )
        db.commit()
        outbox = _load_outbox_by_dedupe(db, dedupe_key)
    else:
        outbox.recipient = recipient
        outbox.payload = followup_payload
        outbox.updated_by = user.id
        outbox.updated_at = now
        db.add(outbox)
        db.commit()
    if outbox is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Follow-up delivery outbox missing")
    delivery_state, current = _deliver_rent_followup_outbox_row(db, outbox_id=outbox.id, now=now)
    if delivery_state == "sent":
        task = _sync_rent_followup_assignment(
            db,
            task_id=task.id,
            assignee_user_id=payload.assignee_user_id,
            actor_id=user.id,
            now=now,
        )
        message_id = getattr(current, "telegram_message_id", None) if current is not None else None
        return TaskFollowupDeliveryOut(
            task=_task_read(db, task),
            delivery_state="DELIVERED",
            detail="Follow-up delivered",
            telegram_message_id=message_id,
        )
    current_task = _scoped_get_task(db, task.id, org_id)
    if delivery_state == "failed":
        return TaskFollowupDeliveryOut(
            task=_task_read(db, current_task),
            delivery_state="FAILED",
            detail="Follow-up delivery failed",
            telegram_message_id=None,
        )
    return TaskFollowupDeliveryOut(
        task=_task_read(db, current_task),
        delivery_state="PROCESSING",
        detail="Follow-up delivery is already in progress",
        telegram_message_id=None,
    )


@router.post("/tasks", response_model=TaskActionOut, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Create a V2 task from a conversation event. Dedupes on dedupe_key when
    supplied (at most one active task per key); otherwise a fresh row."""
    org_id = membership.organization_id
    if _self_filter_required(user, membership):
        if payload.assigned_user_id not in (None, user.id):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "SECRETARY may only assign tasks to themselves",
            )
    if payload.property_id is not None:
        if payload.property_id not in _org_property_ids(db, org_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    _check_assignee(db, payload.assigned_user_id)
    _check_property(db, payload.property_id)
    now = datetime.now(timezone.utc)
    fields: dict = {
        "task_type": payload.task_type,
        "title": payload.title,
        "description": payload.description,
        "property_id": payload.property_id,
        "priority": payload.priority,
        "due_at": payload.due_at or (now + timedelta(days=1)),
        "next_action": payload.next_action,
        "next_check_at": payload.next_check_at,
        "context": payload.context,
        "completion_condition": payload.completion_condition,
        "source_event": payload.source_event,
        "source_type": "conversation",
        "assigned_user_id": payload.assigned_user_id,
        "status": (
            OperationalTaskStatus.IN_PROGRESS
            if payload.status == OperationalTaskStatus.IN_PROGRESS
            else OperationalTaskStatus.PENDING
        ),
    }
    if payload.dedupe_key:
        fields["dedupe_key"] = payload.dedupe_key
    if fields["status"] == OperationalTaskStatus.IN_PROGRESS and (
        not payload.next_action or not payload.next_check_at
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "IN_PROGRESS requires next_action and next_check_at",
        )
    task, _ = create_operational_task(
        db, fields=fields, now=now, actor_id=user.id,
    )
    db.commit()
    if task is None:
        where_scope = _scoped_task_query(db, org_id)
        existing = (
            db.query(OperationalTask)
            .filter(
                OperationalTask.dedupe_key == payload.dedupe_key,
                OperationalTask.status == OperationalTaskStatus.PENDING,
                where_scope,
            )
            .first()
        )
        if existing is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Task dedupe conflict")
        return TaskActionOut(task=_task_read(db, existing), detail="Task already exists")
    db.refresh(task)
    return TaskActionOut(task=_task_read(db, task), detail="Task created")


# ---------------------------------------------------------------------------
# PASAY-V2-FOUNDATION-001: deterministic Quick Views + Daily Digest (no LLM)
# ---------------------------------------------------------------------------

@router.get("/quick/tasks")
def quick_tasks(
    scope: str | None = Query(default=None, description="'owner' = Owner attention filter"),
    db: Session = Depends(get_db),
    reader: User | SystemReader = Depends(get_operations_reader),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    if isinstance(reader, SystemReader):
        if scope == "owner":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "SYSTEM reader cannot use the owner scope"
            )
        rows = quick_svc.build_quick_tasks(db, reader, owner_only=False)
        org_prop_ids = _org_property_ids(db, org_id)
        org_lease_ids = _org_lease_ids(db, org_id)
        task_ids = [
            r["id"] for r in rows
            if isinstance(r, dict) and isinstance(r.get("id"), int)
        ]
        allowed_task_ids: set[int] = set()
        if task_ids:
            prop_q = db.execute(
                select(OperationalTask.id).where(
                    OperationalTask.id.in_(task_ids),
                    OperationalTask.property_id.in_(org_prop_ids),
                )
            ).all()
            lease_q = db.execute(
                select(OperationalTask.id).where(
                    OperationalTask.id.in_(task_ids),
                    OperationalTask.lease_id.in_(org_lease_ids),
                )
            ).all()
            orphan_q = db.execute(
                select(OperationalTask.id).where(
                    OperationalTask.id.in_(task_ids),
                    OperationalTask.property_id.is_(None),
                    OperationalTask.lease_id.is_(None),
                )
            ).all()
            allowed_task_ids = (
                {r[0] for r in prop_q}
                | {r[0] for r in lease_q}
                | {r[0] for r in orphan_q}
            )
        payable_expense_ids = [
            r["expense_id"] for r in rows
            if isinstance(r, dict)
            and r.get("kind") == "payable_expense"
            and isinstance(r.get("expense_id"), int)
        ]
        allowed_payable_ids: set[int] = set()
        if payable_expense_ids:
            pay_q = db.execute(
                select(Expense.id).where(
                    Expense.id.in_(payable_expense_ids),
                    Expense.property_id.in_(org_prop_ids),
                )
            ).all()
            allowed_payable_ids = {r[0] for r in pay_q}
        kept: list[dict] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            rid = r.get("id")
            if isinstance(rid, int) and rid in allowed_task_ids:
                kept.append(r)
                continue
            if r.get("kind") == "payable_expense":
                eid = r.get("expense_id")
                if isinstance(eid, int) and eid in allowed_payable_ids:
                    kept.append(r)
        return kept
    return quick_svc.build_quick_tasks(db, reader, owner_only=(scope == "owner"))


@router.get("/quick/properties")
def quick_properties(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    org_prop_ids = _org_property_ids(db, org_id)
    return quick_svc.build_quick_properties(db, org_property_ids=org_prop_ids)


@router.get("/quick/rent")
def quick_rent(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    org_prop_ids = _org_property_ids(db, org_id)
    return quick_svc.build_quick_rent(db, org_property_ids=org_prop_ids)


@router.get("/quick/expense")
def quick_expense(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    org_prop_ids = _org_property_ids(db, org_id)
    return quick_svc.build_quick_expense(db, org_property_ids=org_prop_ids)


@router.get("/quick/expense-duplicates")
def quick_expense_duplicates(
    expense_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Possible-duplicate matcher for one payable expense (PASAY-V2
    -EXPENSE-PAYABLE-TASK-006 §7/§8).

    Advisory only: returns OTHER highly similar PAID expenses (same unit,
    amount, purpose/category and a relevant date window) so the bot can warn
    the Owner before finalizing payment. Amount alone is never a match, and
    no business record is ever deleted or rejected here."""
    from app.models.financial import Expense

    org_id = membership.organization_id
    org_prop_ids = _org_property_ids(db, org_id)
    if not org_prop_ids:
        return []
    expense = (
        db.query(Expense)
        .filter(Expense.id == expense_id, Expense.property_id.in_(list(org_prop_ids)))
        .first()
    )
    if expense is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense not found")
    return quick_svc.find_similar_paid_expenses(db, expense)


@router.get("/quick/unit-timeline")
def quick_unit_timeline(
    unit_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """AI-OPS-FOUNDATION-001 §15: the unit's digital file — deterministic
    time-ordered timeline (rent/payment history, expenses, repairs/tasks,
    evidence, lease events) for NL queries like 'Give me the history of 1608'."""
    org_id = membership.organization_id
    org_prop_ids = _org_property_ids(db, org_id)
    if org_prop_ids:
        unit_owned = (
            db.execute(
                select(Unit.id).where(
                    Unit.id == unit_id,
                    Unit.property_id.in_(list(org_prop_ids)),
                    Unit.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
        )
        if unit_owned is None:
            return []
    else:
        return []
    return quick_svc.build_unit_timeline(db, unit_id)


@router.get("/remind-owner-target")
def remind_owner_target(
    db: Session = Depends(get_db),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """ZERO-LEARNING-004 §4: the canonical HUMAN Owner's Telegram DM target.

    🔔 Remind Owner is a REAL action: the bot DMs the Owner (private chat)
    and only marks the reminder delivered AFTER the DM succeeds. This endpoint
    resolves the Owner's telegram chat id from the canonical human identity
    (admin role + active Telegram binding), so the bot never hardcodes a
    chat id and never falls back to the group.

    Returns ``{"telegram_chat_id": "5177241442"}`` or 404 when no Owner with a
    Telegram destination exists (fail closed — the caller must NOT report the
    reminder as delivered)."""
    org_id = membership.organization_id

    owner = (
        db.query(User)
        .join(Membership, Membership.user_id == User.id)
        .filter(
            Membership.organization_id == org_id,
            Membership.role == OrganizationRole.OWNER,
            Membership.state == MembershipState.ACTIVE,
            Membership.removed_at.is_(None),
            User.is_active.is_(True),
            User.telegram_chat_id.isnot(None),
        )
        .order_by(User.id)
        .first()
    )
    if owner is None or not str(owner.telegram_chat_id).strip():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Owner Telegram destination configured")
    return {"telegram_chat_id": str(owner.telegram_chat_id).strip()}


@router.get("/secretary-target")
def secretary_target(
    db: Session = Depends(get_db),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §2.2/§9: resolve the canonical HUMAN
    Secretary's Telegram DM target for a real ``📞 催租`` assign-to-Secretary DM.

    Exactly mirrors ``/remind-owner-target``: the bot DMs the Secretary
    (private chat) and only marks the follow-up "assigned" AFTER the DM
    succeeds. The recipient is resolved from the canonical human identity —
    the designated Secretary assignee (``OPERATIONS_SECRETARY_ASSIGNEE``) or,
    failing that, the active manager user with a Telegram binding — never a
    hard-coded chat id and never the group.

    Returns ``{"telegram_chat_id": "1083657401"}`` or 404 when no Secretary
    with a Telegram destination exists (fail closed — the caller must NOT
    report the follow-up as assigned)."""
    from app.services.identity import resolve_telegram_destination
    from app.services.operations.config import SECRETARY_ASSIGNEE_ID

    org_id = membership.organization_id
    org_member_user_ids = {
        r[0]
        for r in db.execute(
            select(Membership.user_id).where(
                Membership.organization_id == org_id,
                Membership.state == MembershipState.ACTIVE,
                Membership.removed_at.is_(None),
            )
        ).all()
    }
    if not org_member_user_ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Secretary Telegram destination configured")

    preferred_id = SECRETARY_ASSIGNEE_ID
    for candidate_id in [preferred_id, None]:
        if candidate_id is not None:
            if candidate_id not in org_member_user_ids:
                continue
            cand = db.get(User, candidate_id)
        else:
            cand = (
                db.query(User)
                .join(Membership, Membership.user_id == User.id)
                .filter(
                    Membership.organization_id == org_id,
                    Membership.role == OrganizationRole.SECRETARY,
                    Membership.state == MembershipState.ACTIVE,
                    Membership.removed_at.is_(None),
                    User.is_active.is_(True),
                    User.telegram_chat_id.isnot(None),
                )
                .order_by(User.id)
                .first()
            )
        if cand is None or not cand.is_active:
            continue
        try:
            chat_id = resolve_telegram_destination(db, cand.id)
        except LookupError:
            legacy = str(cand.telegram_chat_id or "").strip()
            if not legacy:
                continue
            chat_id = legacy
        if chat_id:
            return {"telegram_chat_id": str(chat_id).strip(),
                    "principal_id": cand.id}
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No Secretary Telegram destination configured")


@router.get("/digest")
def daily_digest(
    db: Session = Depends(get_db),
    reader: User | SystemReader = Depends(get_operations_reader),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    org_id = membership.organization_id
    org_prop_ids = _org_property_ids(db, org_id)
    return quick_svc.build_digest(db, reader, org_property_ids=org_prop_ids)


@router.get("/summary", response_model=OperationsSummary)
def operations_summary(
    scope: str | None = Query(default=None, description="'owner' = Owner attention filter"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Operational counts for the current user (agents scoped to their own)."""
    from app.services.operations.summary import build_operations_summary

    _ = membership.organization_id
    return build_operations_summary(db, user, owner_only=(scope == "owner"))


# ---------------------------------------------------------------------------
# recurring rules
# ---------------------------------------------------------------------------

def _get_rule_or_404(db: Session, rule_id: int) -> RecurringRule:
    rule = (
        db.query(RecurringRule)
        .filter(RecurringRule.id == rule_id, RecurringRule.deleted_at.is_(None))
        .first()
    )
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recurring rule not found")
    return rule


def _scoped_get_rule(db: Session, rule_id: int, org_id: int) -> RecurringRule:
    org_prop_ids = _org_property_ids(db, org_id)
    q = db.query(RecurringRule).filter(
        RecurringRule.id == rule_id,
        RecurringRule.deleted_at.is_(None),
    )
    if org_prop_ids:
        q = q.filter(RecurringRule.property_id.in_(list(org_prop_ids)))
    else:
        q = q.filter(RecurringRule.id == -1)
    rule = q.first()
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recurring rule not found")
    return rule


@router.get("/rules", response_model=list[RecurringRuleRead])
def list_rules(
    enabled: bool | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    _forbid_agent_role(user)
    org_id = membership.organization_id
    org_prop_ids = _org_property_ids(db, org_id)
    query = db.query(RecurringRule).filter(RecurringRule.deleted_at.is_(None))
    if org_prop_ids:
        query = query.filter(RecurringRule.property_id.in_(list(org_prop_ids)))
    else:
        query = query.filter(RecurringRule.id == -1)
    if enabled is not None:
        query = query.filter(RecurringRule.enabled.is_(enabled))
    return query.order_by(RecurringRule.id).all()


@router.post("/rules", response_model=RecurringRuleRead, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: RecurringRuleCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    _forbid_agent_role(user)
    org_id = membership.organization_id
    if payload.property_id is not None:
        if payload.property_id not in _org_property_ids(db, org_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    _check_assignee(db, payload.assigned_user_id)
    _check_property(db, payload.property_id)
    obj = RecurringRule(**payload.model_dump(exclude={"next_run_at"}))
    obj.next_run_at = payload.next_run_at or datetime.now(timezone.utc)
    obj.created_by = user.id
    obj.updated_by = user.id
    db.add(obj)
    db.flush()
    record_audit(
        db,
        table_name="recurring_rules",
        record_id=obj.id,
        action="rule_created",
        actor_id=user.id,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.patch("/rules/{rule_id}", response_model=RecurringRuleRead)
def update_rule(
    rule_id: int,
    payload: RecurringRuleUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    _forbid_agent_role(user)
    org_id = membership.organization_id
    obj = _scoped_get_rule(db, rule_id, org_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return obj
    if updates.get("property_id") is not None:
        if updates["property_id"] not in _org_property_ids(db, org_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")
    _check_assignee(db, updates.get("assigned_user_id"))
    _check_property(db, updates.get("property_id"))
    old = serialize_row(obj)
    for field, value in updates.items():
        setattr(obj, field, value)
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="recurring_rules",
        record_id=obj.id,
        action="rule_updated",
        actor_id=user.id,
        old_value=old,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


@router.post("/rules/{rule_id}/disable", response_model=RecurringRuleRead)
def disable_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    _forbid_agent_role(user)
    org_id = membership.organization_id
    obj = _scoped_get_rule(db, rule_id, org_id)
    if not obj.enabled:
        return obj
    old = serialize_row(obj)
    obj.enabled = False
    obj.updated_by = user.id
    record_audit(
        db,
        table_name="recurring_rules",
        record_id=obj.id,
        action="rule_disabled",
        actor_id=user.id,
        old_value=old,
        new_value=serialize_row(obj),
    )
    db.commit()
    db.refresh(obj)
    return obj


# ---------------------------------------------------------------------------
# scheduler trigger
# ---------------------------------------------------------------------------

@router.post("/scheduler/run", response_model=SchedulerRunResult)
def trigger_scheduler(
    db: Session = Depends(get_db),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Run one scheduler pass on demand (same code path as the worker loop).

    Fail fast if the default assignee is misconfigured, so an operator-triggered
    pass can never silently create un-notifiable business tasks (mirrors the
    worker's startup guard — worker.main() validates at boot; this endpoint
    validates on hit).
    """
    from app.services.operations.config import DEFAULT_ASSIGNED_USER_ID
    from app.services.operations.assignee import validate_default_assignee

    _ = membership.organization_id
    validate_default_assignee(db, DEFAULT_ASSIGNED_USER_ID)
    return run_scheduler_once(db)


# ---------------------------------------------------------------------------
# copilot context + action proposals (V1.2.2 Phase B — NO execution)
# ---------------------------------------------------------------------------

@router.get("/copilot/context")
def copilot_context(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Deterministic, RBAC-scoped Copilot context.

    Admin/manager see the full operational picture; agents see only their own
    tasks and the entities reachable through them. Read-only: the only write is
    the ``copilot_runs`` audit row for this build. No LLM is involved and
    nothing executes any action in Phase A+B.
    """
    _ = membership.organization_id
    context = copilot_svc.build_copilot_context(db, user)
    copilot_svc.log_context_run(db, actor=user, context=context)
    db.commit()
    return context


@router.post(
    "/copilot/today",
    response_model=CopilotTodayOut,
    status_code=status.HTTP_200_OK,
)
def copilot_today(
    payload: CopilotTodayIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Read-only TODAY brief (C1.1 deterministic-first).

    DEFAULT (no ``provider``): deterministic fast path — the grounded A+B
    context + the existing ``ranking.rank_items`` engine, top-3 items, and a
    versioned deterministic summary. NO LLM on the critical path: no provider
    call, no timeout, no failure path; returns in milliseconds.

    Explicit ``provider`` (eval/measurement): LLM enrichment via
    ``build_today``, post-validated server-side (grounded refs, top-K
    restriction, <=3 items, <=2 sentences). Fail-closed: provider errors
    (unreachable/timeout/5xx) return 503 — never a fabricated answer.

    The only DB write is the optional ``copilot_runs`` audit row.
    """
    body = payload or CopilotTodayIn()
    provider = body.provider
    if provider is not None and provider not in copilot_llm.list_providers():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown copilot LLM provider {provider!r}; "
            f"known providers: {', '.join(copilot_llm.list_providers())}",
        )

    if provider is None:
        # Deterministic-first: this is the immediate Telegram response.
        result = copilot_today_fast_svc.build_today_deterministic(db, user)
        copilot_svc.log_context_run(
            db,
            actor=user,
            context={
                "context": result.context,
                "today": {
                    "top_items": [item.to_dict() for item in result.top_items],
                    "summary": result.summary,
                    "summary_version": result.summary_version,
                    "provider": result.provider,
                    "model": result.model,
                    "latency_ms": result.latency_ms,
                    "enriched": result.enriched,
                    "flags": result.flags,
                    "deterministic_top_refs": result.deterministic_top_refs,
                    "latency": result.latency.to_dict(),
                },
                "intent_note": body.intent_note,
            },
            intent="copilot_today",
        )
        db.commit()
        return CopilotTodayOut(
            top_items=[item.to_dict() for item in result.top_items],
            summary=result.summary,
            context_schema_version=result.context_schema_version,
            provider=result.provider,
            model=result.model,
            latency_ms=result.latency_ms,
            enriched=result.enriched,
            summary_version=result.summary_version,
            flags=result.flags,
            deterministic_top_refs=result.deterministic_top_refs,
            latency=LatencyOut(**result.latency.to_dict()),
        )

    try:
        result = copilot_today_svc.build_today(db, user, provider=provider)
    except copilot_llm.LLMProviderError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": f"copilot provider unavailable: {exc}",
                "error_code": "llm_provider_error",
            },
        ) from exc
    except copilot_today_svc.TodayError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": f"copilot produced no usable brief: {exc}",
                "error_code": "copilot_today_unavailable",
            },
        ) from exc
    # Optional audit row (the only C1 write): what the copilot was shown + the
    # validated brief + internal flags (hallucinated/dropped/backfilled refs).
    copilot_svc.log_context_run(
        db,
        actor=user,
        context={
            "context": result.context,
            "today": {
                "top_items": [item.to_dict() for item in result.top_items],
                "summary": result.summary,
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "enriched": result.enriched,
                "flags": result.flags,
                "deterministic_top_refs": result.deterministic_top_refs,
                "latency": result.latency.to_dict(),
            },
            "intent_note": body.intent_note,
        },
        intent="copilot_today",
    )
    db.commit()
    return CopilotTodayOut(
        top_items=[item.to_dict() for item in result.top_items],
        summary=result.summary,
        context_schema_version=result.context_schema_version,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency.total_ms,
        enriched=result.enriched,
        flags=result.flags,
        deterministic_top_refs=result.deterministic_top_refs,
        latency=LatencyOut(**result.latency.to_dict()),
    )


@router.post(
    "/copilot/why",
    response_model=CopilotWhyOut,
    status_code=status.HTTP_200_OK,
)
def copilot_why(
    payload: CopilotWhyIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Per-item WHY enrichment (on-demand LLM, deterministic fail-closed).

    Grounds from ``build_copilot_context``, validates ``item_ref`` is in the
    grounded set, and calls the EXPLAIN provider profile (fast non-reasoning
    by default) with a scoped WHY prompt. On provider error/timeout/malformed
    output returns HTTP 200 with ``fallback=True`` and the DETERMINISTIC
    reason + suggested action from the priority engine — never fabricated.
    LLM amounts/dates are post-validated against the grounded facts and
    stripped + flagged when invented. Read-only; the only write is the
    optional ``copilot_runs`` audit row.
    """
    provider = payload.provider
    if provider is not None and provider not in copilot_llm.list_providers():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown copilot LLM provider {provider!r}; "
            f"known providers: {', '.join(copilot_llm.list_providers())}",
        )
    try:
        result = copilot_why_svc.explain_item(
            db, user, payload.item_ref, provider=provider
        )
    except copilot_why_svc.WhyItemNotGrounded as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    copilot_svc.log_context_run(
        db,
        actor=user,
        context={
            "context": result.context,
            "why": {
                "item_ref": result.item_ref,
                "explanation": result.explanation,
                "recommendation": result.recommendation,
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "fallback": result.fallback,
                "flags": result.flags,
                "latency": result.latency.to_dict(),
            },
        },
        intent="copilot_why",
    )
    db.commit()
    return CopilotWhyOut(
        item_ref=result.item_ref,
        explanation=result.explanation,
        recommendation=result.recommendation,
        grounded_refs=result.grounded_refs,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency.total_ms,
        fallback=result.fallback,
        flags=result.flags,
        latency=LatencyOut(**result.latency.to_dict()),
    )


@router.post(
    "/copilot/ask",
    response_model=CopilotAskOut,
    status_code=status.HTTP_200_OK,
)
def copilot_ask(
    payload: CopilotAskIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """On-demand Q&A enrichment (grounded, deterministic fail-closed).

    Grounds to the current A+B context and calls the ASK provider profile
    (strong model by default). LLM output is post-validated server-side: any
    amount/date not resolvable in the grounded context is stripped + flagged,
    backend refs are removed, nothing is executed or written. On provider-down
    returns HTTP 200 with ``fallback=True`` and a friendly deterministic
    answer — never fabricated. Read-only; the only write is the optional
    ``copilot_runs`` audit row.
    """
    provider = payload.provider
    if provider is not None and provider not in copilot_llm.list_providers():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown copilot LLM provider {provider!r}; "
            f"known providers: {', '.join(copilot_llm.list_providers())}",
        )
    try:
        result = copilot_ask_svc.ask_question(
            db, user, payload.question, provider=provider
        )
    except copilot_ask_svc.AskQuestionRequired as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    copilot_svc.log_context_run(
        db,
        actor=user,
        context={
            "context": result.context,
            "ask": {
                "question": payload.question,
                "answer": result.answer,
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "fallback": result.fallback,
                "flags": result.flags,
                "latency": result.latency.to_dict(),
            },
        },
        intent="copilot_ask",
    )
    db.commit()
    return CopilotAskOut(
        answer=result.answer,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency.total_ms,
        fallback=result.fallback,
        flags=result.flags,
        latency=LatencyOut(**result.latency.to_dict()),
    )


@router.post(
    "/copilot/nl-parse",
    response_model=CopilotNlParseOut,
    status_code=status.HTTP_200_OK,
)
def copilot_nl_parse(
    payload: CopilotNlParseIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """BOT-V1-USABLE-001 P0-5: grounded NL intent parsing for the bot's AI
    fallback lane.

    The text is grounded to the real catalog (units / tenants / categories /
    current month) and parsed into a STRUCTURED intent. Nothing is executed
    or written here; the bot maps the intent to its existing deterministic
    business paths. Provider-down returns HTTP 200 with ``fallback=True`` and
    a deterministic classification / clarification — never a fabricated
    write action. Read-only; the only write is the optional ``copilot_runs``
    audit row.
    """
    provider = payload.provider
    if provider is not None and provider not in copilot_llm.list_providers():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown copilot LLM provider {provider!r}; "
            f"known providers: {', '.join(copilot_llm.list_providers())}",
        )
    result = copilot_nl_parse_svc.parse_nl_intent(
        db, user, payload.text, provider=provider
    )
    copilot_svc.log_context_run(
        db,
        actor=user,
        context={
            "text": payload.text,
            "nl_parse": {
                "intent": result.intent,
                "unit": result.unit,
                "unit_id": result.unit_id,
                "amount": str(result.amount) if result.amount is not None else None,
                "category": result.category,
                "month": result.month,
                "missing": result.missing,
                "options": result.options,
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "fallback": result.fallback,
                "flags": result.flags,
            },
        },
        intent="copilot_nl_parse",
    )
    db.commit()
    return CopilotNlParseOut(
        intent=result.intent,
        message=result.message,
        unit=result.unit,
        unit_id=result.unit_id,
        amount=str(result.amount) if result.amount is not None else None,
        category=result.category,
        month=result.month,
        missing=result.missing,
        options=result.options,
        provider=result.provider,
        model=result.model,
        fallback=result.fallback,
        flags=result.flags,
        latency_ms=result.latency_ms,
    )




def _target_label(db: Session, target_type: str, target_id: int) -> str:
    """Human label for the confirmation card (rendering only)."""
    from app.models.tenant import Tenant
    from app.models.property import Unit
    target = copilot_svc._resolve_target(db, target_type, target_id)
    if target is None:
        return ""
    if target_type == "lease":
        unit = db.get(Unit, target.unit_id)
        tenant = db.get(Tenant, target.tenant_id)
        return (
            f"Lease #{target.id} · {unit.unit_number if unit else '?'} · "
            f"{tenant.full_name if tenant else '?'}"
        )
    if target_type == "property":
        return f"{target.name} (#{target.id})"
    if target_type == "task":
        return f"#{target.id} {target.title}"
    return f"{target_type} #{target_id}"


def _build_recommend_card(db: Session, proposal: CopilotActionProposal) -> CopilotProposalCard:
    """Render-safe card data (the bot must not display the raw proposal id)."""
    payload = proposal.payload_json or {}
    assignee_id = payload.get("assignee_user_id")
    assignee_name = None
    if assignee_id is not None:
        assignee = db.get(User, assignee_id)
        assignee_name = assignee.username if assignee else None
    due = None
    for key in ("due_at", "until"):
        raw = payload.get(key)
        if isinstance(raw, str):
            try:
                due = datetime.fromisoformat(raw)
            except ValueError:
                due = None
            break
    return CopilotProposalCard(
        action_type=proposal.action_type,
        target_type=proposal.target_type,
        target_id=proposal.target_id,
        target_label=_target_label(db, proposal.target_type, proposal.target_id),
        reason_code=payload.get("reason_code"),
        assignee_user_id=assignee_id,
        assignee_name=assignee_name,
        due_at=due,
        note=payload.get("note"),
        display_context=payload.get("display_context") or {},
    )


@router.post(
    "/copilot/recommend",
    response_model=CopilotRecommendOut,
    status_code=status.HTTP_201_CREATED,
)
def copilot_recommend(
    payload: CopilotRecommendIn,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Canonical proposal builder endpoint (V1.2.2 Phase C2).

    The bot posts an intent + resolved refs; the backend deterministically
    resolves EVERYTHING (action code, target, assignee, due time, property
    scope, idempotency key) and returns the canonical PENDING proposal card.
    Nothing executes here — the owner must tap the confirmation card, then
    POST /copilot/proposals/{id}/execute.
    """
    try:
        proposal, created, _payload = copilot_proposals.build_proposal_from_intent(
            db,
            actor=user,
            intent=payload.intent,
            source_type=payload.source_type,
            source_id=payload.source_id,
            task_ref=payload.task_ref,
            reason_code=payload.reason_code,
            assignee_user_id=payload.assignee_user_id,
            due_at=payload.due_at,
            preset=payload.preset,
            note=payload.note,
        )
    except copilot_proposals.ProposalNeedsClarification as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except copilot_svc.ProposalValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    db.refresh(proposal)
    if not created:
        response.status_code = status.HTTP_200_OK
    return CopilotRecommendOut(
        proposal_id=proposal.id,
        action_type=proposal.action_type,
        status=proposal.status.value,
        target_type=proposal.target_type,
        target_id=proposal.target_id,
        idempotency_key=proposal.idempotency_key,
        expires_at=proposal.expires_at,
        card=_build_recommend_card(db, proposal),
        detail=(
            "Proposal already exists (idempotent replay)"
            if not created
            else "Proposal created"
        ),
        created=created,
    )


@router.post(
    "/copilot/proposals/{proposal_id}/execute",
    response_model=CopilotExecuteOut,
)
def execute_copilot_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """CONFIRMED -> EXECUTED (V1.2.2 Phase C2).

    Fail-closed, in ONE DB transaction: the proposal row is locked and
    EVERYTHING is revalidated against CURRENT state (kill-switch, actor
    existence/activity/permission/ownership, EXACT executable allowlist,
    payload schema, target existence/scope/staleness, assignee eligibility,
    snooze window), then the mutation routes through the EXISTING operations
    service layer and the proposal is marked EXECUTED. Replay when already
    EXECUTED returns the existing result with ``replay=true`` and creates no
    second effect. Rejections return a structured 409
    ``{"message", "error_code"}`` with a ``copilot_proposal_execution_rejected``
    audit and NO mutation.
    """
    before = db.get(CopilotActionProposal, proposal_id)
    before_status = before.status if before is not None else None
    try:
        proposal = copilot_execute.execute_proposal(
            db, actor=user, proposal_id=proposal_id
        )
    except copilot_execute.ExecutionDisabledError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except copilot_execute.ProposalExecuteRejectedError as exc:
        db.commit()  # persist the execution_rejected audit (+ EXPIRED transition)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "error_code": exc.error_code},
        ) from exc
    except copilot_svc.ProposalStateError as exc:
        raise _proposal_state_error(exc) from exc
    db.commit()
    db.refresh(proposal)
    was_replay = before_status == CopilotActionStatus.EXECUTED
    return CopilotExecuteOut(
        proposal=CopilotProposalRead.model_validate(proposal),
        result=copilot_execute.execution_result(db, proposal, replay=was_replay),
    )


def _proposal_state_error(exc: Exception) -> HTTPException:
    if "not found" in str(exc):
        return HTTPException(status.HTTP_404_NOT_FOUND, "Copilot proposal not found")
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.post(
    "/copilot/proposals",
    response_model=CopilotProposalActionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_copilot_proposal(
    payload: CopilotProposalCreate,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Create a PENDING action proposal.

    Idempotency is actor-scoped (``UNIQUE(actor_user_id, idempotency_key)``):
    a duplicate submission by the SAME actor returns the existing proposal with
    HTTP 200 instead of creating a second row; a different actor using the same
    key is an independent request. ``action_type`` / ``target_type`` /
    ``idempotency_key`` are canonicalized (NFC + invisible-character removal)
    at this boundary. Nothing is executed — proposals only record intent for
    Phase C.
    """
    try:
        proposal, created = copilot_svc.create_proposal(
            db,
            actor=user,
            action_type=payload.action_type,
            target_type=payload.target_type,
            target_id=payload.target_id,
            payload=payload.payload,
            idempotency_key=payload.idempotency_key,
            expires_at=payload.expires_at,
        )
    except copilot_svc.ProposalValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    db.refresh(proposal)
    if not created:
        response.status_code = status.HTTP_200_OK
    return CopilotProposalActionOut(
        proposal=CopilotProposalRead.model_validate(proposal),
        detail=(
            "Proposal already exists (idempotent replay)"
            if not created
            else "Proposal created"
        ),
    )


@router.post(
    "/copilot/proposals/{proposal_id}/confirm",
    response_model=CopilotProposalActionOut,
)
def confirm_copilot_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Confirm a PENDING proposal (PENDING -> CONFIRMED).

    Fail-closed: in one DB transaction the proposal row is locked and
    EVERYTHING is revalidated against current state (actor existence/activity/
    permission, proposal state + expiry, target allowlist/existence/scope,
    action x target legality, payload schema, business staleness). Failures
    return a structured 409 ``{"message", "error_code"}``, write a
    ``copilot_proposal_confirm_rejected`` audit, and execute nothing. Idempotent
    replay when already CONFIRMED; expired proposals are atomically marked
    EXPIRED and rejected. Phase A+B never transitions to EXECUTED and never
    sets ``executed_at``.
    """
    before = db.get(CopilotActionProposal, proposal_id)
    before_status = (
        before.status if before is not None else None
    )  # snapshot value BEFORE the service mutates the same ORM instance
    try:
        proposal = copilot_svc.confirm_proposal(db, actor=user, proposal_id=proposal_id)
    except copilot_svc.ProposalExpiredError as exc:
        db.commit()  # persist the EXPIRED transition before rejecting
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except copilot_svc.ProposalConfirmRejectedError as exc:
        db.commit()  # persist the copilot_proposal_confirm_rejected audit
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "error_code": exc.error_code},
        ) from exc
    except copilot_svc.ProposalStateError as exc:
        raise _proposal_state_error(exc) from exc
    db.commit()
    db.refresh(proposal)
    was_replay = before_status == CopilotActionStatus.CONFIRMED
    return CopilotProposalActionOut(
        proposal=CopilotProposalRead.model_validate(proposal),
        detail=(
            "Proposal already confirmed (idempotent replay)"
            if was_replay
            else "Proposal confirmed"
        ),
    )


@router.post(
    "/copilot/proposals/{proposal_id}/cancel",
    response_model=CopilotProposalActionOut,
)
def cancel_copilot_proposal(
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """Cancel a PENDING proposal (PENDING -> CANCELLED, idempotent replay)."""
    before = db.get(CopilotActionProposal, proposal_id)
    before_status = (
        before.status if before is not None else None
    )  # snapshot value BEFORE the service mutates the same ORM instance
    try:
        proposal = copilot_svc.cancel_proposal(db, actor=user, proposal_id=proposal_id)
    except copilot_svc.ProposalExpiredError as exc:
        db.commit()  # persist the EXPIRED transition before rejecting
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except copilot_svc.ProposalStateError as exc:
        raise _proposal_state_error(exc) from exc
    db.commit()
    db.refresh(proposal)
    was_replay = before_status == CopilotActionStatus.CANCELLED
    return CopilotProposalActionOut(
        proposal=CopilotProposalRead.model_validate(proposal),
        detail=(
            "Proposal already cancelled (idempotent replay)"
            if was_replay
            else "Proposal cancelled"
        ),
    )


# ---------------------------------------------------------------------------
# PASAY-AI-EMPLOYEE-FOUNDATION-007: Rent Action Pack + self-healing + conflicts
# ---------------------------------------------------------------------------

@router.get("/action-pack")
def rent_action_pack(
    unit_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """§11-§15: the FULL Rent Action Pack for a unit — tenant phone, real
    outstanding / periods / overdue days, last follow-up, latest payment
    promise, payment method, and call+message scripts built ENTIRELY from
    structured truth (never LLM-fabricated). ``assignable`` is False (with
    ``blocked_hint``) when the tenant phone is missing/invalid — the caller
    must NOT hand a collection job to the Secretary in that case (§12)."""
    from app.services.operations.rent_pack import build_rent_action_pack

    org_id = membership.organization_id
    org_prop_ids = _org_property_ids(db, org_id)
    if org_prop_ids:
        unit_owned = (
            db.execute(
                select(Unit.id).where(
                    Unit.id == unit_id,
                    Unit.property_id.in_(list(org_prop_ids)),
                    Unit.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
        )
        if unit_owned is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")

    pack = build_rent_action_pack(db, unit_id)
    if pack.get("error") == "unit_not_found":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    if pack.get("error") == "no_active_lease":
        raise HTTPException(status.HTTP_409_CONFLICT, "Unit has no active lease")
    return pack


@router.get("/conflict-report")
def conflict_report(
    unit_id: int = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """§10: deterministic DATA-CONFLICT report for one unit — never silently
    chooses; returns human-resolvable options for rent-vs-legacy conflicts."""
    from app.services.operations.conflicts import build_conflict_report

    org_id = membership.organization_id
    org_prop_ids = _org_property_ids(db, org_id)
    if org_prop_ids:
        unit_owned = (
            db.execute(
                select(Unit.id).where(
                    Unit.id == unit_id,
                    Unit.property_id.in_(list(org_prop_ids)),
                    Unit.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
        )
        if unit_owned is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    return build_conflict_report(db, unit_id)


@router.get("/route")
def action_route(
    action_type: str = Query(..., description="RENT_FOLLOWUP / EXPENSE_OWNER_PAYMENT"),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """§19: resolve the canonical responsibility for an action type
    (RENT_FOLLOWUP -> SECRETARY, EXPENSE_OWNER_PAYMENT -> OWNER). Fails closed
    for not-yet-routed types."""
    from app.services.operations.action_router import (
        RouteNotRouted,
        route_action,
        route_code,
    )

    try:
        code = route_code(action_type)
    except (RouteNotRouted, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)
        ) from exc
    return {"action_type": str(action_type), "route": code, "responsibility": route_action(action_type).value}


@router.post("/promise", response_model=PaymentPromiseOut, status_code=status.HTTP_201_CREATED)
def record_payment_promise(
    payload: PaymentPromiseIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """§17: persist a REAL payment promise (amount / promised_date / recorded_by)
    on the unit's active rent task so the workflow auto-checks it at the
    promised date. This is a HIGH-risk enter-the-workflow action only in the
    sense it starts a scheduled check — the promise itself is a commitment
    (not money), so no financial write; the bot confirms before calling."""
    from datetime import datetime, timezone

    from app.models.lease import Lease
    from app.models.operations import OperationalTask, OperationalTaskType, OperationalTaskStatus
    from app.services.operations.promises import apply_payment_promise

    org_id = membership.organization_id
    org_lease_ids = _org_lease_ids(db, org_id)
    lease = None
    if payload.lease_id is not None:
        lease_q = db.query(Lease).filter(Lease.id == payload.lease_id)
        if org_lease_ids:
            lease_q = lease_q.filter(Lease.id.in_(list(org_lease_ids)))
        else:
            lease_q = lease_q.filter(Lease.id == -1)
        lease = lease_q.first()
    if lease is None:
        if payload.lease_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "lease_id is required")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lease not found")

    where_scope = _scoped_task_query(db, org_id)
    task = (
        db.query(OperationalTask)
        .filter(
            where_scope,
            OperationalTask.lease_id == lease.id,
            OperationalTask.task_type.in_(
                [OperationalTaskType.RENT_OVERDUE, OperationalTaskType.FOLLOWUP]
            ),
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .order_by(OperationalTask.id)
        .first()
    )
    if task is None:
        # No active task: create a RENT_OVERDUE one to carry the promise.
        from app.services.operations.generation import create_operational_task

        task, _ = create_operational_task(
            db,
            fields={
                "task_type": OperationalTaskType.RENT_OVERDUE,
                "title": f"Collect overdue rent · {lease.unit_id}",
                "lease_id": lease.id,
                "tenant_id": lease.tenant_id,
                "source_type": "lease",
                "source_id": lease.id,
                "status": OperationalTaskStatus.PENDING,
                "due_at": datetime.now(timezone.utc),
                "next_action": "Follow up with tenant for promised payment.",
                "dedupe_key": f"lease:{lease.id}:RENT_OVERDUE",
            },
            now=datetime.now(timezone.utc),
            actor_id=user.id,
        )
        if task is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Promise task de-duplicated elsewhere")

    amount = None
    if payload.amount is not None:
        from decimal import Decimal

        amount = Decimal(str(payload.amount)).quantize(Decimal("0.01"))
    from datetime import timezone as _tz

    apply_payment_promise(
        db,
        task,
        amount=amount,
        promised_date=payload.promised_date.astimezone(_tz.utc),
        recorded_by=user.id,
        note=payload.note or "",
    )
    record_audit(
        db,
        table_name="operational_tasks",
        record_id=task.id,
        action="payment_promise_recorded",
        actor_id=user.id,
        changed_fields={"details.promise": [None, dict(task.details or {}).get("promise")]},
        old_value=serialize_row(task),
        new_value=serialize_row(task),
    )
    db.commit()
    return PaymentPromiseOut(
        task_id=task.id,
        amount=str(amount) if amount is not None else None,
        promised_date=payload.promised_date.astimezone(_tz.utc).isoformat(),
        recorded_by=user.id,
        status="open",
    )


@router.post("/resume", response_model=ResumeActionOut)
def resume_blocked(
    payload: ResumeActionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """§2 / §8: self-healing. The human supplied the missing low-risk data
    (e.g. ``tenant_phone``); this saves it, resolves the block on any matching
    task and returns the ``blocked_action`` so the caller auto-executes it —
    the user NEVER re-clicks the original action."""
    from app.models.lease import Lease
    from app.models.operations import OperationalTask, OperationalTaskStatus
    from app.models.tenant import Tenant
    from app.services.operations import resolver as resolver_svc
    from app.services.operations.resolver import task_blocked

    org_id = membership.organization_id
    org_prop_ids = _org_property_ids(db, org_id)
    org_lease_ids = _org_lease_ids(db, org_id)
    org_tenant_ids = _org_tenant_ids(db, org_id)
    lease_id = payload.lease_id
    unit_id = payload.unit_id
    task = None

    if payload.task_id is not None:
        task = _scoped_get_task(db, payload.task_id, org_id)

    if task is None and lease_id is not None:
        if not org_lease_ids or lease_id not in org_lease_ids:
            pass
        else:
            where_scope = _scoped_task_query(db, org_id)
            task = (
                db.query(OperationalTask)
                .filter(
                    where_scope,
                    OperationalTask.lease_id == lease_id,
                    OperationalTask.status.in_(
                        [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
                    ),
                )
                .order_by(OperationalTask.id)
                .first()
            )

    # 1) Apply the low-risk direct write.
    message_parts = []
    if payload.field == "tenant_phone" and payload.value:
        lease = None
        if lease_id is not None:
            from app.models.lease import Lease as _Lease

            q = db.query(_Lease).filter(_Lease.id == lease_id)
            if org_lease_ids:
                q = q.filter(_Lease.id.in_(list(org_lease_ids)))
            else:
                q = q.filter(_Lease.id == -1)
            lease = q.first()
        if lease is None and unit_id is not None:
            from app.models.lease import Lease as _Lease

            q = (
                db.query(_Lease)
                .filter(_Lease.unit_id == unit_id, _Lease.status == "active",
                        _Lease.deleted_at.is_(None))
            )
            if org_prop_ids:
                q = q.join(Unit, Unit.id == _Lease.unit_id).filter(
                    Unit.property_id.in_(list(org_prop_ids))
                )
            else:
                q = q.filter(_Lease.id == -1)
            lease = q.first()
        tenant = None
        if lease is not None:
            if org_tenant_ids and lease.tenant_id in org_tenant_ids:
                tenant = db.get(Tenant, lease.tenant_id)
        if tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found for the block")
        old_phone = tenant.phone
        tenant.phone = (payload.value or "").strip()
        from app.models.tenant import TenantContactStatus

        if tenant.contact_status == TenantContactStatus.WRONG_NUMBER:
            tenant.contact_status = TenantContactStatus.UNVERIFIED
        if tenant.contact_status is None:
            tenant.contact_status = TenantContactStatus.UNVERIFIED
        tenant.updated_by = user.id
        record_audit(
            db,
            table_name="tenants",
            record_id=tenant.id,
            action="phone_direct_update",
            actor_id=user.id,
            changed_fields={"phone": [old_phone, tenant.phone]},
            old_value=serialize_row(tenant),
            new_value=serialize_row(tenant),
        )
        message_parts.append(f"已记录租客电话：{tenant.phone}")

    # 2) Resolve the block on the matched task.
    blocked_action = None
    if task is not None and task_blocked(task):
        blocked_action = resolver_svc.resolve_issue(task)
        if blocked_action:
            record_audit(
                db,
                table_name="operational_tasks",
                record_id=task.id,
                action="blocked_resolved",
                actor_id=user.id,
                changed_fields={"details.blocked": ["present", "resolved"]},
                old_value=serialize_row(task),
                new_value=serialize_row(task),
            )

    db.commit()
    resolved = bool(blocked_action is not None) or bool(
        task is not None and not task_blocked(task)
    )
    return ResumeActionOut(
        resolved=resolved,
        blocked_action=blocked_action,
        message=" ".join(message_parts) or (
            "资料已保存；请执行下一步。" if blocked_action else "无阻塞项待恢复。"
        ),
    )


@router.get("/resolver/issues")
def list_resolver_issues(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    membership: Membership = Depends(resolve_org_membership(role=[OrganizationRole.OWNER, OrganizationRole.SECRETARY])),
):
    """§8: current blocked issues across active tasks (what is blocking, why,
    and the one-line fix each) — the Owner / dashboard can enumerate dead-ends
    that still need a human input to unblock self-healing."""
    from app.models.operations import OperationalTask, OperationalTaskStatus
    from app.services.operations.resolver import task_blocked

    org_id = membership.organization_id
    where_scope = _scoped_task_query(db, org_id)
    active = (
        db.query(OperationalTask)
        .filter(
            where_scope,
            OperationalTask.status.in_(
                [OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS]
            ),
        )
        .all()
    )
    issues = []
    for t in active:
        b = task_blocked(t)
        if not b:
            continue
        issues.append(
            {
                "task_id": t.id,
                "unit_id": t.lease_id,
                "issue_type": b.get("issue_type"),
                "field": b.get("field"),
                "blocked_action": b.get("blocked_action"),
                "risk_level": b.get("risk_level"),
                "suggested_fix": b.get("suggested_fix"),
                "created_at": b.get("created_at"),
            }
        )
    return {"issues": issues}
