"""V1 SYSTEM scheduled-job operations surface.

Issue #119 P0 fix (independent review): the production
``PASSAY_JOB_API_KEY`` authenticates the real job calls in
``pasay_bot/jobs.py`` — ``GET /operations/digest`` and
``GET /operations/quick/tasks`` — neither of which is served by the
existing V1 ``/api/v1/operations/*`` router (which only exposes the
V1 ``Operation`` / ``Task`` CRUD + ``/operations/{id}/notify``).

The legacy ``app/api/routers/operations.py`` exposes those two
endpoints, but the production entrypoint is now ``app.v1.main:app``
(not ``app.main:app``), and the legacy routers depend on legacy
``users`` / ``principals`` / ``api_credentials`` tables that do NOT
exist in the production ``v1_*`` schema. We therefore re-implement
the two scheduled-job endpoints in V1 against the real production
schema, using V1 SYSTEM principal auth (``get_system_principal``)
so the same ``PASSAY_JOB_API_KEY`` (after provisioning through
``scripts/create_v1_api_key.py``) authenticates the real
``pasay_bot/jobs.py`` calls.

The endpoints return the SHAPE the bot already consumes:

  * ``/operations/digest`` — three-section digest
    (``act_now`` / ``upcoming`` / ``done_today``) plus the legacy
    ``pending`` / ``in_progress`` / ``recently_completed`` keys for
    backward compatibility with ``cards.active_tasks_digest_card``.

  * ``/operations/quick/tasks`` — flat list of active tasks
    (PENDING + IN_PROGRESS), each row carrying the
    ``next_check_at`` / ``due_at`` / ``property_code`` fields the
    bot's ``task_event_card`` reads.

Both endpoints are READ-ONLY. ``get_system_principal`` never
resolves to a HUMAN Principal, ``require_role`` rejects
SystemPrincipal, and these routes never call ``db.commit()`` on a
write path. SYSTEM credentials cannot reach a write (the second leg
of the JOB-SERVICE-AUTH-002 carryover guarantee).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.permissions import (
    PermissionDenied,
    Principal,
    Role,
    SystemPrincipal,
    require_org_scope,
    require_system_org_scope,
)
from app.v1.deps import (
    get_db_dep,
    get_human_or_system_principal,
    get_system_principal,
)
from app.v1.models.base import TaskState
from app.v1.models.expense import (
    ExpenseClaim,
    ExpenseClaimStatus,
)
from app.v1.models.property import Unit
from app.v1.models.rent_payment import (
    Operation,
    RentDueSchedule,
    RentDueState,
    Task,
)
from app.v1.models.tenant_lease import Lease


router = APIRouter(prefix="/operations", tags=["system-ops"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


_MAX_DIGEST_ACT = 8
_MAX_DIGEST_UPCOMING = 5
_MAX_DIGEST_DONE = 3


def _unit_label(unit: Unit | None) -> str | None:
    """Display label for a unit, None if missing."""
    if unit is None:
        return None
    label = getattr(unit, "label", None)
    return str(label) if label else None


def _row_from_task(
    db: Session, task: Task, *,
    now: datetime,
) -> dict[str, Any]:
    """Serialize a V1 Task into the legacy ``active_tasks_digest_card`` /
    ``task_event_card`` contract. Stable keys only — no nested ORM.
    """
    due_at: datetime | None = task.due_at
    overdue_days: int | None = None
    due_in_days: int | None = None
    if due_at is not None:
        if due_at < now:
            overdue_days = max((now - due_at).days, 0)
        else:
            due_in_days = (due_at - now).days
    op = db.get(Operation, task.operation_id) if task.operation_id else None
    unit_id = None
    if op is not None and op.subject_type == "unit" and op.subject_id:
        unit_id = int(op.subject_id)
    unit: Unit | None = (
        db.get(Unit, unit_id) if unit_id is not None else None
    )
    return {
        "id": int(task.id),
        "task_type": str(task.kind),
        "title": str(task.title),
        "status": (
            "PENDING" if task.state == TaskState.OPEN.value
            else "DONE" if task.state == TaskState.DONE.value
            else "CANCELLED"
        ),
        "due_at": due_at.isoformat() if due_at is not None else None,
        "next_action": None,
        "next_check_at": due_at.isoformat() if due_at is not None else None,
        "property_code": _unit_label(unit),
        "operation_id": int(task.operation_id) if task.operation_id else None,
        "overdue_days": overdue_days,
        "due_in_days": due_in_days,
    }


# ---------------------------------------------------------------------------
# /operations/quick/tasks — active tasks for the digest / next_check job
# ---------------------------------------------------------------------------


@router.get("/quick/tasks")
def quick_tasks(
    org_id: int | None = Query(
        default=None,
        gt=0,
        description=(
            "Optional caller-supplied target org_id. For SYSTEM "
            "credentials it MUST match the credential's "
            "trusted_organization_id; otherwise 403. For HUMAN "
            "credentials the org is derived from the credential's "
            "active membership."
        ),
    ),
    scope: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal=Depends(get_human_or_system_principal),
    db: Session = Depends(get_db_dep),
) -> dict[str, Any]:
    """Active tasks (PENDING + IN_PROGRESS) for both the SYSTEM
    scheduled job and the OWNER menu path.

    Issue #119 P0 (independent review follow-up): the canonical target
    org is the SYSTEM credential's ``trusted_organization_id``. The
    ``org_id`` query parameter is OPTIONAL for SYSTEM — when supplied
    it MUST match the bound org (mismatch → 403). When absent, the
    server uses the credential's bound org directly. This is the
    single-source-of-truth binding that prevents a leaked SYSTEM key
    from reading across every org in the database.

    Issue #119 P0 (Telegram six-menu V1 contract repair): the same
    endpoint also serves the OWNER's ``✅ 待办`` menu path. The
    ``get_human_or_system_principal`` dep accepts either credential
    type; for HUMAN callers the active membership row scopes the read.
    """
    if isinstance(principal, SystemPrincipal):
        try:
            canonical_org_id = require_system_org_scope(principal, org_id)
        except PermissionDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        if scope == "owner":
            # The legacy /operations/quick/tasks?scope=owner path
            # filters to owner-actionable tasks. The V1 system surface
            # does not implement the owner-actionable filter (it is a
            # HUMAN-caller concept); a SYSTEM caller asking for
            # ``scope=owner`` is a contract mismatch — fail closed.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "SYSTEM reader cannot use the owner scope; "
                "the owner filter requires a Human Principal",
            )
    else:
        # HUMAN caller: derive org from membership.
        canonical_org_id = int(principal.org_id)
        if org_id is not None and org_id != canonical_org_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"cross-org access denied: principal org_id="
                f"{principal.org_id} target org_id={org_id}",
            )
        if scope == "owner" and principal.role != Role.OWNER.value:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "scope=owner requires OWNER role",
            )

    now = datetime.now(timezone.utc)
    q = (
        db.query(Task)
        .filter(
            Task.org_id == canonical_org_id,
            Task.state == TaskState.OPEN.value,
        )
        .order_by(Task.due_at.is_(None), Task.due_at, Task.id)
    )
    total = q.count()
    rows = q.offset(offset).limit(limit).all()
    items = [_row_from_task(db, t, now=now) for t in rows]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ---------------------------------------------------------------------------
# /operations/digest — daily tasks digest for the SYSTEM digest job
# ---------------------------------------------------------------------------


def _act_now_rows(
    db: Session, org_id: int, *, now: datetime,
) -> list[dict[str, Any]]:
    """Build the ``act_now`` section from V1 RentDueSchedule (overdue) +
    V1 ExpenseClaim (submitted / verified). Same shape the bot's
    ``cards.active_tasks_digest_card`` reads.
    """
    overdue_schedules = (
        db.query(RentDueSchedule)
        .filter(
            RentDueSchedule.org_id == org_id,
            RentDueSchedule.state.in_(
                (RentDueState.DUE.value, RentDueState.OVERDUE.value)
            ),
            RentDueSchedule.due_date < now,
        )
        .order_by(RentDueSchedule.due_date, RentDueSchedule.id)
        .all()
    )
    rows: list[dict[str, Any]] = []
    for sched in overdue_schedules:
        lease = db.get(Lease, sched.lease_id) if sched.lease_id else None
        unit = db.get(Unit, lease.unit_id) if lease and lease.unit_id else None
        days_overdue = max((now.date() - sched.due_date).days, 0) if sched.due_date else 0
        rows.append(
            {
                "kind": "rent_overdue",
                "task_id": int(sched.id),
                "unit": _unit_label(unit) or "",
                "amount": str(sched.amount_due),
                "due_at": sched.due_date.isoformat() if sched.due_date else None,
                "days_overdue": days_overdue,
                "sort_anchor": -days_overdue,
                "sort_tie": int(sched.id),
            }
        )

    pending_expenses = (
        db.query(ExpenseClaim)
        .filter(
            ExpenseClaim.org_id == org_id,
            ExpenseClaim.status.in_(
                (ExpenseClaimStatus.SUBMITTED.value,
                 ExpenseClaimStatus.VERIFIED.value)
            ),
        )
        .order_by(ExpenseClaim.created_at, ExpenseClaim.id)
        .all()
    )
    for claim in pending_expenses:
        rows.append(
            {
                "kind": "payable_expense",
                "task_id": int(claim.id),
                "unit": "",
                "amount": str(claim.claimed_amount),
                "title": str(claim.title),
                "sort_anchor": -int(claim.id),
                "sort_tie": int(claim.id),
            }
        )
    rows.sort(
        key=lambda r: (
            0 if r["kind"] == "rent_overdue" else 1,
            -r.get("sort_anchor", 0),
            -r.get("sort_tie", 0),
        ),
    )
    return rows


def _upcoming_rows(
    db: Session, org_id: int, *, now: datetime,
) -> list[dict[str, Any]]:
    """Near-term rent-due rows (watch, do not chase)."""
    horizon = now.date()
    due_schedules = (
        db.query(RentDueSchedule)
        .filter(
            RentDueSchedule.org_id == org_id,
            RentDueSchedule.state == RentDueState.DUE.value,
            RentDueSchedule.due_date >= horizon,
        )
        .order_by(RentDueSchedule.due_date, RentDueSchedule.id)
        .limit(_MAX_DIGEST_UPCOMING)
        .all()
    )
    rows: list[dict[str, Any]] = []
    for sched in due_schedules:
        lease = db.get(Lease, sched.lease_id) if sched.lease_id else None
        unit = db.get(Unit, lease.unit_id) if lease and lease.unit_id else None
        rows.append(
            {
                "kind": "rent_due_upcoming",
                "task_id": int(sched.id),
                "unit": _unit_label(unit) or "",
                "amount": str(sched.amount_due),
                "due_at": sched.due_date.isoformat() if sched.due_date else None,
            }
        )
    return rows


def _done_today_rows(
    db: Session, org_id: int, *, now: datetime,
) -> list[dict[str, Any]]:
    """Tasks completed today (V1: Task.state == DONE with done_at today)."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    done_tasks = (
        db.query(Task)
        .filter(
            Task.org_id == org_id,
            Task.state == TaskState.DONE.value,
            Task.done_at >= today_start,
            Task.done_at <= now,
        )
        .order_by(Task.done_at, Task.id)
        .limit(_MAX_DIGEST_DONE)
        .all()
    )
    rows: list[dict[str, Any]] = []
    for t in done_tasks:
        op = db.get(Operation, t.operation_id) if t.operation_id else None
        unit_id = (
            int(op.subject_id)
            if op is not None and op.subject_type == "unit" and op.subject_id
            else None
        )
        unit = db.get(Unit, unit_id) if unit_id is not None else None
        rows.append(
            {
                "kind": "task_done",
                "task_id": int(t.id),
                "unit": _unit_label(unit) or "",
                "title": str(t.title),
                "done_at": t.done_at.isoformat() if t.done_at else None,
            }
        )
    return rows


@router.get("/digest")
def daily_digest(
    org_id: int | None = Query(
        default=None,
        gt=0,
        description=(
            "Optional caller-supplied target org_id. For SYSTEM "
            "credentials it MUST match the credential's "
            "trusted_organization_id; otherwise 403. For HUMAN "
            "credentials the org is derived from the credential's "
            "active membership."
        ),
    ),
    principal=Depends(get_human_or_system_principal),
    db: Session = Depends(get_db_dep),
) -> dict[str, Any]:
    """Daily Tasks Digest — three sections (act_now / upcoming / done_today)
    plus the legacy ``pending`` / ``in_progress`` / ``recently_completed``
    keys the bot's ``active_tasks_digest_card`` reads as a fallback.

    Issue #119 P0 (independent review follow-up): the canonical target
    org is the SYSTEM credential's ``trusted_organization_id``. The
    ``org_id`` query parameter is OPTIONAL for SYSTEM — when supplied
    it MUST match the bound org (mismatch → 403). When absent, the
    server uses the credential's bound org directly. The bot's
    ``PasayApiClient.get_digest()`` therefore does not need to know
    the org id at all; the credential carries it.

    Issue #119 P0 (Telegram six-menu V1 contract repair): the same
    endpoint also serves the OWNER's ``✅ 待办`` menu path. The
    ``get_human_or_system_principal`` dep accepts either credential
    type; for HUMAN callers the active membership row scopes the read.
    """
    if isinstance(principal, SystemPrincipal):
        try:
            canonical_org_id = require_system_org_scope(principal, org_id)
        except PermissionDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    else:
        # HUMAN caller: derive org from membership.
        canonical_org_id = int(principal.org_id)
        if org_id is not None and org_id != canonical_org_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"cross-org access denied: principal org_id="
                f"{principal.org_id} target org_id={org_id}",
            )

    now = datetime.now(timezone.utc)
    act_now = _act_now_rows(db, canonical_org_id, now=now)
    upcoming = _upcoming_rows(db, canonical_org_id, now=now)
    done_today = _done_today_rows(db, canonical_org_id, now=now)

    act_hidden = max(len(act_now) - _MAX_DIGEST_ACT, 0)
    upcoming_hidden = max(len(upcoming) - _MAX_DIGEST_UPCOMING, 0)
    done_hidden = max(len(done_today) - _MAX_DIGEST_DONE, 0)

    # Legacy contract — the bot falls back to these keys when the
    # semantic sections are empty (see pasay_bot/render/cards.py).
    pending = [
        {
            "id": -i,
            "task_type": str(r["kind"]).upper(),
            "title": str(r["kind"]),
            "status": "PENDING",
        }
        for i, r in enumerate(act_now)
    ]
    in_progress: list[dict[str, Any]] = []
    recently_completed = [
        {
            "id": -i,
            "task_type": str(r["kind"]).upper(),
            "title": str(r["title"]),
            "status": "COMPLETED",
            "completed_by": 1,
        }
        for i, r in enumerate(done_today)
    ]
    return {
        "act_now": act_now[:_MAX_DIGEST_ACT],
        "upcoming": upcoming[:_MAX_DIGEST_UPCOMING],
        "done_today": done_today[:_MAX_DIGEST_DONE],
        "hidden": {
            "act_now": act_hidden,
            "upcoming": upcoming_hidden,
            "done_today": done_hidden,
        },
        "counts": {
            "act_now": len(act_now),
            "upcoming": len(upcoming),
            "done_today": len(done_today),
        },
        # Legacy fallback keys (bot's active_tasks_digest_card).
        "pending": pending,
        "in_progress": in_progress,
        "recently_completed": recently_completed,
    }


__all__ = ["router"]
