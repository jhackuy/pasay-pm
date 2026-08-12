"""Deterministic Copilot Context + Action Proposal Safety (V1.2.2 Phase B/C2).

Phase A+B built the read-only context + proposal lifecycle. Phase C2 (this
module) authorizes the EXECUTABLE allowlist — EXACTLY three actions:

    create_followup_task / assign_task / snooze_task

Action mapping (documented, enforced here AND by the DB CHECK):
- ``create_followup_task`` is the canonical EXECUTABLE follow-up code. The
  legacy ``follow_up`` / ``create_task`` codes keep their READ-side proposal
  semantics (they may still be proposed/confirmed/cancelled) but are NOT
  executable — the executor rejects them with ``ERR_ACTION_NOT_EXECUTABLE``.
- READ actions (``summarize`` / ``analyze`` / ``explain`` / ``risk_scan``) are
  never executable.
- ABSENT action codes — any financial verb (income confirm/reverse, expense
  approve/reject/pay/reverse, settlement confirm), COMPLETE/CANCEL variants,
  unknown strings, and confusable/Unicode variants — are rejected with a stable
  ``error_code`` and a ``copilot_proposal_*_rejected`` audit at every boundary.
  ``canonicalize()`` (NFC + invisible-character strip) is the first gate.

Execution is gated by ``COPILOT_EXECUTION_ENABLED`` (env-driven, default
False). When False the executor refuses (fail closed). Every execution path
passes through the flag; there is no autonomous (no-confirm) path.

Financial mutation is structurally impossible: the ``action_type`` allowlist
contains no financial-write verb, an OPERATIONAL proposal may not target a
financial entity (expense / income / settlement), the executor only calls the
operations task service layer (no financial service reachable), and payloads
may not carry SQL / execution / bypass keys. Financial intents route through
the existing V1.1 state machine (no second financial write path).

The security boundary is ARCHITECTURAL, not lexical: the Copilot surface has
no raw DB access and no SQL execution — it only ever calls parameterized
backend services against a structured schema with enum allowlists. The SQL /
execution keyword denylist is defense-in-depth only (a cosmetic guard), never
the security boundary, and is NOT a substitute for the allowlist + schema.

Context contract (stable for Phase C):
- ``context_schema_version = "1.0"`` identifies the schema.
- Every list is size-capped and deterministically ordered (see
  ``CONTEXT_CAPS`` / ``CONTEXT_ORDERING``; both are echoed inside the payload).
- Free text from the DB is DATA, never instruction: it is returned under
  clearly-named data keys, ``free_text_policy = "data_only"`` lists exactly
  which fields are free text, and no key holds executable code.
- No API keys, tokens, or unrelated PII are ever included.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.commission import CommissionSettlement, CommissionSettlementStatus
from app.models.copilot import (
    CopilotActionProposal,
    CopilotActionStatus,
    CopilotRun,
    CopilotRunStatus,
)
from app.models.financial import Expense, ExpenseStatus, Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.operations import (
    OperationalTask,
    OperationalTaskPriority,
    OperationalTaskStatus,
    RecurringRule,
)
from app.models.property import Property, Unit
from app.models.task import Task, TaskStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.models.identity import Principal, PrincipalType
from app.config import settings
from app.services.audit import audit_context, record_audit, serialize_row
from app.services.operations.config import LEASE_EXPIRY_WINDOW_DAYS
from app.services.operations.rent_math import covered_periods, lease_periods
from app.services.operations.summary import build_operations_summary

# ---------------------------------------------------------------------------
# Context schema + policy constants (documented, echoed in the payload)
# ---------------------------------------------------------------------------

CONTEXT_SCHEMA_VERSION = "1.0"
COPILOT_TIMEZONE = "Asia/Manila"

# Action Safety Matrix (design targets, enforced NOW before Phase C):
#   READ         -> analyze/summarize/explain/risk-scan; may auto-execute later.
#   OPERATIONAL  -> proposal + explicit user confirm.
#   FINANCIAL    -> never executable by the Copilot; NOT in the allowlist.
#                   Financial intents must route through the V1.1 state machine.
ACTION_SAFETY: dict[str, str] = {
    "summarize": "READ",
    "analyze": "READ",
    "explain": "READ",
    "risk_scan": "READ",
    "create_task": "OPERATIONAL",
    "assign_task": "OPERATIONAL",
    "snooze_task": "OPERATIONAL",
    "follow_up": "OPERATIONAL",
    "create_followup_task": "OPERATIONAL",
}
READ_ACTIONS = frozenset(a for a, s in ACTION_SAFETY.items() if s == "READ")
OPERATIONAL_ACTIONS = frozenset(a for a, s in ACTION_SAFETY.items() if s == "OPERATIONAL")
# V1.2.2 Phase C2: the EXACT executor allowlist. ONLY these three codes may
# ever transition a proposal to EXECUTED; everything else (including legacy
# create_task / follow_up and every financial verb) is rejected at execute
# time with ERR_ACTION_NOT_EXECUTABLE.
EXECUTABLE_ACTIONS = frozenset({"create_followup_task", "assign_task", "snooze_task"})
FINANCIAL_TARGET_TYPES = frozenset({"expense", "income", "settlement"})
TARGET_TYPES = frozenset({"property", "lease", "task", "expense", "income", "settlement"})

# Payload denylist: keys that could smuggle raw SQL / execution / a financial
# mutation bypass into a proposal payload are rejected outright. Top-level
# proposal fields are also blocked so the payload can never shadow them.
# NOTE: this denylist is DEFENSE-IN-DEPTH ONLY — the real boundary is the
# structured schema + enum allowlists + parameterized backend services (the
# Copilot never reaches raw SQL). Do not treat this list as a security boundary.
PAYLOAD_DENYLIST_KEYS = frozenset({
    "sql", "raw_sql", "query", "statement", "script", "execute", "exec",
    "financial_write", "bypass", "bypass_safety",
    "action_type", "status", "target_type", "target_id",
    # V1.2.2 Phase C2: financial / irreversible verbs can never be smuggled
    # into an executable payload key (defense-in-depth only).
    "approve", "confirm_income", "financial", "reverse", "settle",
    "complete", "cancel", "status_transition",
})
PAYLOAD_MAX_BYTES = 16 * 1024  # 16 KiB serialized-JSON cap
IDEMPOTENCY_KEY_MAX_LENGTH = 128

# Canonical form for allowlist / idempotency comparisons at the API boundary:
# Unicode NFC normalization + removal of invisible / zero-width control
# characters, so ``"\u200bsummarize"`` and confusable variants can never
# defeat the action_type / target_type allowlists or split an idempotency key.
_INVISIBLE_CHARS = re.compile(
    "[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]"
)


def canonicalize(value: str) -> str:
    """NFC-normalize and strip invisible/zero-width characters."""
    if not isinstance(value, str):
        return value
    return _INVISIBLE_CHARS.sub("", unicodedata.normalize("NFC", value))


# Stable machine-readable error codes for confirm-time revalidation (V1.2.2
# A+B.1). The router surfaces these as a structured 409 body with
# ``{"message": ..., "error_code": ...}``. Codes are append-only.
ERR_ACTOR_NOT_FOUND = "actor_not_found"
ERR_ACTOR_INACTIVE = "actor_inactive"
ERR_ACTOR_PERMISSION = "actor_permission"
ERR_PROPOSAL_STATE = "proposal_state"
ERR_PROPOSAL_EXPIRED = "proposal_expired"
ERR_TARGET_TYPE_UNKNOWN = "target_type_unknown"
ERR_TARGET_MISSING = "target_missing"
ERR_TARGET_OUT_OF_SCOPE = "target_out_of_scope"
ERR_ACTION_TARGET_ILLEGAL = "action_target_illegal"
ERR_PAYLOAD_INVALID = "payload_invalid"
ERR_BUSINESS_STALE = "business_stale"
# V1.2.2 Phase C2 execute-time codes (append-only).
ERR_ACTION_NOT_EXECUTABLE = "action_not_executable"
ERR_ASSIGNEE_INVALID = "assignee_invalid"
ERR_SNOOZE_WINDOW_INVALID = "snooze_window_invalid"

# V1.2.2 Phase C2: execution is enabled ONLY when the environment says so
# (``COPILOT_EXECUTION_ENABLED``; default False in .env.example and app/config).
# One source of truth: env wins, module default matches .env.example. The
# executor refuses (fail closed) while this is False. ``COPILOT_EXECUTION_ENABLED``
# is kept in sync with ``settings.copilot_execution_enabled`` (same env var).
COPILOT_EXECUTION_ENABLED = settings.copilot_execution_enabled

# Deterministic size caps (top-N per section).
CONTEXT_CAPS: dict[str, int] = {
    "pending_tasks": 25,
    "overdue_rents": 25,
    "leases_expiring": 25,
    "pending_expense_approvals": 25,
    "pending_settlements": 25,
    "maintenance_tasks": 25,
    "recurring_rules": 25,
    "properties": 50,
    "tenants": 50,
    "income_references": 50,
}

# Deterministic ordering rules (stable across builds).
CONTEXT_ORDERING: dict[str, str] = {
    "pending_tasks": "due_at asc, priority (critical>high>medium>low), id asc",
    "overdue_rents": "total_outstanding desc, lease_id asc",
    "leases_expiring": "end_date asc, id asc",
    "pending_expense_approvals": "created_at desc, id desc",
    "pending_settlements": "created_at desc, id desc",
    "maintenance_tasks": "due_date asc (nulls last), id asc",
    "recurring_rules": "next_run_at asc, id asc",
    "properties": "name asc, id asc",
    "tenants": "full_name asc, id asc",
    "income_references": "received_date desc, id desc",
}

# Free-text fields: DATA, never instructions (contract for Phase C prompting).
FREE_TEXT_FIELDS = [
    "task.title",
    "task.description",
    "property.name",
    "property.address",
    "lease.notes",
    "tenant.full_name",
    "expense.description",
    "settlement.notes",
]

_PRIORITY_WEIGHT: dict[OperationalTaskPriority, int] = {
    OperationalTaskPriority.critical: 0,
    OperationalTaskPriority.high: 1,
    OperationalTaskPriority.medium: 2,
    OperationalTaskPriority.low: 3,
}
_TWO_PLACES = Decimal("0.01")


class ProposalValidationError(ValueError):
    """Invalid proposal (unknown action, bad target, unsafe payload...)."""


class ProposalStateError(ValueError):
    """Legal proposal that cannot transition (expired, cancelled, conflict...)."""


class ProposalExpiredError(ProposalStateError):
    """The proposal expired; the EXPIRED transition has been applied and must
    be committed by the caller before the error surfaces."""


class ProposalConfirmRejectedError(ProposalStateError):
    """Confirm-time revalidation failed (fail closed): the proposal stays
    PENDING and nothing executes. Carries a stable machine-readable
    ``error_code`` for the API contract."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class ExecutionDisabledError(RuntimeError):
    """``COPILOT_EXECUTION_ENABLED`` is off — the executor refuses (fail
    closed). Raised by ``_guard_execution_disabled`` on every execution path."""



# ---------------------------------------------------------------------------
# Context builder (read-only; see module docstring)
# ---------------------------------------------------------------------------

def build_copilot_context(db: Session, user: User, *, now: datetime | None = None) -> dict:
    """Build the deterministic, RBAC-scoped context for ``user``.

    Agents see only their own tasks and entities reachable through them (plus
    their own settlements); admins/managers see the full operational picture.
    Pure reads: the only table this function writes is never written here —
    the caller writes the ``copilot_runs`` row via ``log_context_run``.
    """
    now = now or datetime.now(timezone.utc)
    is_agent = user.role == UserRole.agent

    tasks = _pending_tasks(db, user)
    task_ids = [t["id"] for t in tasks]
    lease_ids = _scoped_ids(db, tasks, "lease_id")
    property_ids = _scoped_ids(db, tasks, "property_id")
    tenant_ids = _scoped_ids(db, tasks, "tenant_id")
    expense_ids = {
        t["source_id"] for t in tasks if t["source_type"] == "expense" and t["source_id"]
    }
    settlement_ids = {
        t["source_id"]
        for t in tasks
        if t["source_type"] == "commission_settlement" and t["source_id"]
    }

    # Lease scope: for agents, only leases their tasks reference; for
    # privileged users, every active lease.
    lease_filter = lease_ids if is_agent else None

    overdue_rents = _overdue_rents(db, lease_filter, now=now)
    leases_expiring = _leases_expiring(db, lease_filter, now=now)
    expenses = _pending_expenses(db, expense_ids if is_agent else None)
    settlements = _pending_settlements(db, user, settlement_ids if is_agent else None)
    maintenance = _maintenance_tasks(db, user)
    recurring_rules = _recurring_rules(db, user)
    properties = _properties(db, property_ids if is_agent else None)
    tenants = _tenants(db, tenant_ids if is_agent else None)
    income_refs = _income_references(db, lease_filter)

    references = {
        "properties": [f"property:{p['id']}" for p in properties],
        "leases": _dedupe(
            [f"lease:{r['lease_id']}" for r in overdue_rents]
            + [f"lease:{l['id']}" for l in leases_expiring]
            + [f"lease:{s['lease_id']}" for s in settlements]
            + [f"lease:{t['lease_id']}" for t in tasks if t["lease_id"]]
        ),
        "tasks": [f"task:{t['id']}" for t in tasks],
        "expenses": _dedupe(
            [f"expense:{e['id']}" for e in expenses]
            + [
                f"expense:{t['source_id']}"
                for t in tasks if t["source_type"] == "expense" and t["source_id"]
            ]
        ),
        "incomes": income_refs,
        "settlements": [f"settlement:{s['id']}" for s in settlements],
    }

    return {
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "current_time": now.isoformat(),
        "timezone": COPILOT_TIMEZONE,
        "user": {"id": user.id, "role": user.role.value, "username": user.username},
        "scoped_to_user": is_agent,
        "free_text_policy": "data_only",
        "free_text_fields": list(FREE_TEXT_FIELDS),
        "summary": build_operations_summary(db, user, now=now).model_dump(),
        "pending_tasks": tasks,
        "overdue_rents": overdue_rents,
        "leases_expiring": leases_expiring,
        "pending_expense_approvals": expenses,
        "pending_settlements": settlements,
        "maintenance_tasks": maintenance,
        "recurring_rules": recurring_rules,
        "properties": properties,
        "tenants": tenants,
        "references": references,
        "size_caps": dict(CONTEXT_CAPS),
        "ordering": dict(CONTEXT_ORDERING),
    }


def log_context_run(
    db: Session, *, actor: User, context: dict, now: datetime | None = None,
    intent: str = "context_build",
) -> CopilotRun:
    """Persist one context-build audit row (the only context write)."""
    now = now or datetime.now(timezone.utc)
    run = CopilotRun(
        actor_user_id=actor.id,
        intent=intent,
        context_snapshot=context,
        status=CopilotRunStatus.COMPLETED,
        created_by=actor.id,
        updated_by=actor.id,
    )
    db.add(run)
    db.flush()
    record_audit(
        db,
        table_name="copilot_runs",
        record_id=run.id,
        action="copilot_context_built",
        actor_id=actor.id,
        new_value=serialize_row(run),
    )
    return run


def _pending_tasks(db: Session, user: User) -> list[dict]:
    query = db.query(OperationalTask).filter(
        OperationalTask.status == OperationalTaskStatus.PENDING
    )
    if user.role == UserRole.agent:
        query = query.filter(OperationalTask.assigned_user_id == user.id)
    rows = query.all()
    rows.sort(key=lambda t: (t.due_at, _PRIORITY_WEIGHT.get(t.priority, 9), t.id))
    return [
        {
            "id": t.id,
            "task_type": t.task_type.value,
            "title": t.title,
            "description": t.description,
            "priority": t.priority.value,
            "status": t.status.value,
            "due_at": t.due_at.isoformat(),
            "remind_at": t.remind_at.isoformat() if t.remind_at else None,
            "snoozed_until": t.snoozed_until.isoformat() if t.snoozed_until else None,
            "assigned_user_id": t.assigned_user_id,
            "source_type": t.source_type,
            "source_id": t.source_id,
            "property_id": t.property_id,
            "lease_id": t.lease_id,
            "tenant_id": t.tenant_id,
            "reference": f"task:{t.id}",
        }
        for t in rows[: CONTEXT_CAPS["pending_tasks"]]
    ]


def _scoped_ids(db: Session, tasks: list[dict], field: str) -> set[int]:
    _ = db
    return {t[field] for t in tasks if t.get(field) is not None}


def _overdue_rents(db: Session, lease_ids: set[int] | None, *, now: datetime) -> list[dict]:
    """Mirrors /reports/overdue-rents using the shared rent_math module."""
    today = now.date()
    query = db.query(Lease).filter(
        Lease.status == LeaseStatus.active, Lease.deleted_at.is_(None)
    )
    if lease_ids is not None:
        query = query.filter(Lease.id.in_(lease_ids or [0]))
    leases = query.all()
    lease_id_list = [l.id for l in leases]
    confirmed_by_lease: dict[int, list[Income]] = {}
    if lease_id_list:
        for income in (
            db.query(Income)
            .filter(
                Income.lease_id.in_(lease_id_list),
                Income.status == IncomeStatus.confirmed,
            )
            .all()
        ):
            confirmed_by_lease.setdefault(income.lease_id, []).append(income)

    rows: list[dict] = []
    for lease in leases:
        unit = db.get(Unit, lease.unit_id)
        tenant = db.get(Tenant, lease.tenant_id)
        if unit is None or tenant is None:
            continue
        periods = lease_periods(lease)
        due_periods = [(month, due) for month, due in periods if due <= today]
        if not due_periods:
            continue
        covered = covered_periods(lease, periods, confirmed_by_lease.get(lease.id, []))
        overdue = [(month, due) for month, due in due_periods if month not in covered]
        if not overdue:
            continue
        total = _money(Decimal(lease.monthly_rent) * len(overdue))
        rows.append(
            {
                "lease_id": lease.id,
                "unit_id": lease.unit_id,
                "tenant_id": lease.tenant_id,
                "unit": unit.unit_number,
                "tenant": tenant.full_name,
                "overdue_months": len(overdue),
                "amount_per_month": _money(lease.monthly_rent),
                "total_outstanding": total,
                "oldest_due_date": min(due for _, due in overdue).isoformat(),
                "overdue_days": max((today - min(due for _, due in overdue)).days, 0),
                "reference": f"lease:{lease.id}",
            }
        )
    rows.sort(key=lambda r: (-Decimal(r["total_outstanding"]), r["lease_id"]))
    return rows[: CONTEXT_CAPS["overdue_rents"]]


def _leases_expiring(db: Session, lease_ids: set[int] | None, *, now: datetime) -> list[dict]:
    today = now.date()
    horizon = today + timedelta(days=LEASE_EXPIRY_WINDOW_DAYS)
    query = db.query(Lease).filter(
        Lease.status == LeaseStatus.active,
        Lease.deleted_at.is_(None),
        Lease.end_date >= today,
        Lease.end_date <= horizon,
    )
    if lease_ids is not None:
        query = query.filter(Lease.id.in_(lease_ids or [0]))
    rows = query.order_by(Lease.end_date, Lease.id).limit(CONTEXT_CAPS["leases_expiring"]).all()
    return [
        {
            "id": l.id,
            "lease_id": l.id,
            "unit_id": l.unit_id,
            "tenant_id": l.tenant_id,
            "end_date": l.end_date.isoformat(),
            "monthly_rent": _money(l.monthly_rent),
            "reference": f"lease:{l.id}",
        }
        for l in rows
    ]


def _pending_expenses(db: Session, expense_ids: set[int] | None) -> list[dict]:
    query = db.query(Expense).filter(Expense.status == ExpenseStatus.pending)
    if expense_ids is not None:
        query = query.filter(Expense.id.in_(expense_ids or [0]))
    rows = (
        query.order_by(Expense.created_at.desc(), Expense.id.desc())
        .limit(CONTEXT_CAPS["pending_expense_approvals"])
        .all()
    )
    return [
        {
            "id": e.id,
            "expense_date": e.expense_date.isoformat(),
            "category": e.category,
            "amount": _money(e.amount),
            "payee": e.payee,
            "description": e.description,
            "unit_id": e.unit_id,
            "reference": f"expense:{e.id}",
        }
        for e in rows
    ]


def _pending_settlements(
    db: Session, user: User, settlement_ids: set[int] | None
) -> list[dict]:
    query = db.query(CommissionSettlement).filter(
        CommissionSettlement.status == CommissionSettlementStatus.pending
    )
    if user.role == UserRole.agent:
        query = query.filter(
            or_(
                CommissionSettlement.agent_id == user.id,
                CommissionSettlement.id.in_(settlement_ids or [0]),
            )
        )
    rows = (
        query.order_by(CommissionSettlement.created_at.desc(), CommissionSettlement.id.desc())
        .limit(CONTEXT_CAPS["pending_settlements"])
        .all()
    )
    return [
        {
            "id": s.id,
            "agent_id": s.agent_id,
            "lease_id": s.lease_id,
            "computed_amount": _money(s.computed_amount),
            "notes": s.notes,
            "reference": f"settlement:{s.id}",
        }
        for s in rows
    ]


def _maintenance_tasks(db: Session, user: User) -> list[dict]:
    query = db.query(Task).filter(
        Task.deleted_at.is_(None),
        Task.status.in_([TaskStatus.open, TaskStatus.in_progress]),
    )
    if user.role == UserRole.agent:
        query = query.filter(Task.assigned_to == user.id)
    rows = (
        query.order_by(Task.due_date.is_(None), Task.due_date, Task.id)
        .limit(CONTEXT_CAPS["maintenance_tasks"])
        .all()
    )
    return [
        {
            "id": t.id,
            "title": t.title,
            "description": t.description,
            "status": t.status.value,
            "priority": t.priority.value,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "assigned_to": t.assigned_to,
            "reference": f"task:{t.id}",
        }
        for t in rows
    ]


def _recurring_rules(db: Session, user: User) -> list[dict]:
    query = db.query(RecurringRule).filter(
        RecurringRule.enabled.is_(True), RecurringRule.deleted_at.is_(None)
    )
    if user.role == UserRole.agent:
        query = query.filter(RecurringRule.assigned_user_id == user.id)
    rows = (
        query.order_by(RecurringRule.next_run_at, RecurringRule.id)
        .limit(CONTEXT_CAPS["recurring_rules"])
        .all()
    )
    return [
        {
            "id": r.id,
            "rule_type": r.rule_type.value,
            "title": r.title,
            "recurrence": r.recurrence.value,
            "next_run_at": r.next_run_at.isoformat(),
            "assigned_user_id": r.assigned_user_id,
            "reference": f"rule:{r.id}",
        }
        for r in rows
    ]


def _properties(db: Session, property_ids: set[int] | None) -> list[dict]:
    query = db.query(Property).filter(Property.deleted_at.is_(None))
    if property_ids is not None:
        query = query.filter(Property.id.in_(property_ids or [0]))
    rows = query.order_by(Property.name, Property.id).limit(CONTEXT_CAPS["properties"]).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "address": p.address,
            "city": p.city,
            "total_units": p.total_units,
            "is_active": p.is_active,
            "reference": f"property:{p.id}",
        }
        for p in rows
    ]


def _tenants(db: Session, tenant_ids: set[int] | None) -> list[dict]:
    query = db.query(Tenant).filter(Tenant.deleted_at.is_(None))
    if tenant_ids is not None:
        query = query.filter(Tenant.id.in_(tenant_ids or [0]))
    rows = query.order_by(Tenant.full_name, Tenant.id).limit(CONTEXT_CAPS["tenants"]).all()
    return [
        {
            "id": t.id,
            "full_name": t.full_name,
            "phone": t.phone,
            "email": t.email,
            "reference": f"tenant:{t.id}",
        }
        for t in rows
    ]


def _income_references(db: Session, lease_ids: set[int] | None) -> list[str]:
    query = db.query(Income).filter(Income.status == IncomeStatus.confirmed)
    if lease_ids is not None:
        query = query.filter(Income.lease_id.in_(lease_ids or [0]))
    rows = (
        query.order_by(Income.received_date.desc(), Income.id.desc())
        .limit(CONTEXT_CAPS["income_references"])
        .all()
    )
    return [f"income:{i.id}" for i in rows]


def _dedupe(values: list) -> list:
    return list(dict.fromkeys(values))


def _money(value) -> str:
    return str(Decimal(str(value)).quantize(_TWO_PLACES))


# ---------------------------------------------------------------------------
# Action proposals (no execution — Phase C)
# ---------------------------------------------------------------------------

def create_proposal(
    db: Session,
    *,
    actor: User,
    action_type: str,
    target_type: str,
    target_id: int,
    payload: dict,
    idempotency_key: str,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[CopilotActionProposal, bool]:
    """Create a PENDING proposal; returns (proposal, created).

    Idempotency is actor-scoped (``uq_copilot_action_proposals_actor_idempotency``
    = UNIQUE(actor_user_id, idempotency_key)): the same actor re-submitting the
    same logical request returns the existing proposal with ``created=False``,
    while two different actors using the same key are independent requests.

    ``action_type`` / ``target_type`` / ``idempotency_key`` are canonicalized
    (NFC + invisible-character removal) at this boundary before validation, so
    confusable variants cannot bypass the allowlists or split a key.
    """
    now = now or datetime.now(timezone.utc)
    action_type = canonicalize(action_type)
    target_type = canonicalize(target_type)
    idempotency_key = canonicalize(idempotency_key)
    _validate_proposal(
        db,
        action_type=action_type,
        target_type=target_type,
        target_id=target_id,
        payload=payload,
        idempotency_key=idempotency_key,
        expires_at=expires_at,
        now=now,
    )
    stmt = (
        pg_insert(CopilotActionProposal)
        .values(
            actor_user_id=actor.id,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            payload_json=payload,
            status=CopilotActionStatus.PENDING.value,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            created_by=actor.id,
            updated_by=actor.id,
            proposed_principal_id=audit_context.get()[0],
        )
        .on_conflict_do_nothing(index_elements=["actor_user_id", "idempotency_key"])
        .returning(CopilotActionProposal.id)
    )
    row = db.execute(stmt).first()
    if row is not None:
        proposal = db.get(CopilotActionProposal, row[0])
        record_audit(
            db,
            table_name="copilot_action_proposals",
            record_id=proposal.id,
            action="copilot_proposal_created",
            actor_id=actor.id,
            new_value=serialize_row(proposal),
        )
        return proposal, True
    proposal = (
        db.query(CopilotActionProposal)
        .filter(
            CopilotActionProposal.actor_user_id == actor.id,
            CopilotActionProposal.idempotency_key == idempotency_key,
        )
        .one()
    )
    return proposal, False


def _validate_proposal(
    db: Session,
    *,
    action_type: str,
    target_type: str,
    target_id: int,
    payload: dict,
    idempotency_key: str,
    expires_at: datetime | None,
    now: datetime,
) -> None:
    if action_type not in ACTION_SAFETY:
        raise ProposalValidationError(f"unknown action_type '{action_type}'")
    if target_type not in TARGET_TYPES:
        raise ProposalValidationError(f"unknown target_type '{target_type}'")
    # Financial safety: an OPERATIONAL (or any non-READ) action may never point
    # at a financial entity — there is no financial action_type at all, and no
    # proposal can become a second financial write path.
    if ACTION_SAFETY[action_type] != "READ" and target_type in FINANCIAL_TARGET_TYPES:
        raise ProposalValidationError(
            f"action '{action_type}' may not target financial entity '{target_type}'"
        )
    _validate_payload(payload)
    validate_action_payload(action_type, payload)
    if not idempotency_key or len(idempotency_key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ProposalValidationError("idempotency_key is required (max 128 chars)")
    if expires_at is not None and expires_at <= now:
        raise ProposalValidationError("expires_at must be in the future")
    if _resolve_target(db, target_type, target_id) is None:
        raise ProposalValidationError(
            f"target {target_type}:{target_id} does not exist"
        )


def _validate_payload(payload) -> None:
    """Payload schema rules shared by create AND confirm revalidation.

    Keys are canonicalized (NFC + invisible-character removal) before the
    denylist check so zero-width confusables cannot smuggle a rejected key.
    """
    if not isinstance(payload, dict):
        raise ProposalValidationError("payload must be a JSON object")
    bad_keys = sorted(
        k
        for k in payload
        if isinstance(k, str)
        and (
            canonicalize(k).lower() in PAYLOAD_DENYLIST_KEYS
            or canonicalize(k).lower().startswith(("sql", "raw_"))
        )
    )
    if bad_keys:
        raise ProposalValidationError(f"payload contains rejected keys: {bad_keys}")
    size = len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))
    if size > PAYLOAD_MAX_BYTES:
        raise ProposalValidationError(
            f"payload exceeds the {PAYLOAD_MAX_BYTES}-byte cap"
        )


# Strict per-action payload schemas (V1.2.2 Phase C2). Only the three
# EXECUTABLE actions get a structured schema; every critical field
# (assignee_user_id / due_at / until / reason_code) is backend-resolved and
# enum-validated at create, re-validated at confirm, and re-validated at
# execute. Free text (note / display_context) is DATA only — a follow-up task
# can never carry a financial or irreversible verb.
ACTION_PAYLOAD_SCHEMAS: dict[str, frozenset[str]] = {
    "create_followup_task": frozenset(
        {"action", "reason_code", "assignee_user_id", "due_at", "note", "display_context"}
    ),
    "assign_task": frozenset(
        {"action", "assignee_user_id", "note", "display_context"}
    ),
    "snooze_task": frozenset(
        {"action", "until", "preset", "note", "display_context"}
    ),
}
_NOTE_MAX_LENGTH = 2000


def _require_payload_str(payload: dict, key: str, *, max_length: int | None = None) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProposalValidationError(f"payload.{key} must be a non-empty string")
    if max_length is not None and len(value) > max_length:
        raise ProposalValidationError(f"payload.{key} exceeds {max_length} chars")
    return value


def _require_payload_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProposalValidationError(f"payload.{key} must be an integer")
    return value


def _require_payload_dt(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ProposalValidationError(f"payload.{key} must be an ISO-8601 datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProposalValidationError(f"payload.{key} is not a valid ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ProposalValidationError(f"payload.{key} must be timezone-aware")
    return value


def validate_action_payload(action_type: str, payload) -> None:
    """Strict per-action payload schema (beyond the generic denylist).

    No-op for non-executable actions (READ / legacy create_task / follow_up
    keep the generic denylist + size checks). Executable actions reject
    unknown keys, missing/invalid required fields, and a mismatched ``action``
    echo — fail closed at create, confirm AND execute.
    """
    allowed = ACTION_PAYLOAD_SCHEMAS.get(canonicalize(action_type))
    if allowed is None:
        return
    if not isinstance(payload, dict):
        raise ProposalValidationError("payload must be a JSON object")
    for key in payload:
        if not isinstance(key, str) or canonicalize(key) not in allowed:
            raise ProposalValidationError(f"payload contains disallowed key '{key}'")
    echo = payload.get("action")
    if echo is not None and canonicalize(echo) != canonicalize(action_type):
        raise ProposalValidationError("payload.action must match action_type")
    if action_type == "create_followup_task":
        _require_payload_str(payload, "reason_code", max_length=50)
        _require_payload_int(payload, "assignee_user_id")
        _require_payload_dt(payload, "due_at")
    elif action_type == "assign_task":
        _require_payload_int(payload, "assignee_user_id")
    elif action_type == "snooze_task":
        _require_payload_dt(payload, "until")
    if payload.get("note") is not None and (
        not isinstance(payload["note"], str) or len(payload["note"]) > _NOTE_MAX_LENGTH
    ):
        raise ProposalValidationError(f"payload.note must be a string (max {_NOTE_MAX_LENGTH} chars)")


def _resolve_target(db: Session, target_type: str, target_id: int):
    """Entity existence check for proposal targets (grounding / hallucination
    guard). Returns the entity row or None."""
    if target_type == "property":
        return (
            db.query(Property)
            .filter(Property.id == target_id, Property.deleted_at.is_(None))
            .first()
        )
    if target_type == "lease":
        return (
            db.query(Lease)
            .filter(Lease.id == target_id, Lease.deleted_at.is_(None))
            .first()
        )
    if target_type == "task":
        return db.get(OperationalTask, target_id)
    if target_type == "expense":
        return db.get(Expense, target_id)
    if target_type == "income":
        return db.get(Income, target_id)
    if target_type == "settlement":
        return db.get(CommissionSettlement, target_id)
    return None


def _revalidate_proposal_for_confirm(
    db: Session, *, actor: User, proposal: CopilotActionProposal
) -> None:
    """FAIL-CLOSED revalidation of a PENDING proposal at confirm time.

    Runs inside the caller's single DB transaction against CURRENT state
    (fresh DB reads, not the creation-time snapshot). On any failure it
    records a ``copilot_proposal_confirm_rejected`` audit with a stable
    ``error_code`` and raises ``ProposalConfirmRejectedError``; the proposal
    stays PENDING and nothing executes.
    """
    def _reject(error_code: str, reason: str) -> None:
        record_audit(
            db,
            table_name="copilot_action_proposals",
            record_id=proposal.id,
            action="copilot_proposal_confirm_rejected",
            actor_id=actor.id,
            changed_fields={"error_code": error_code, "reason": reason},
        )
        raise ProposalConfirmRejectedError(error_code, reason)

    # 1) actor still exists and is active
    actor_row = db.get(User, actor.id)
    if actor_row is None:
        _reject(ERR_ACTOR_NOT_FOUND, "actor user no longer exists")
    if not actor_row.is_active:
        _reject(ERR_ACTOR_INACTIVE, "actor user is deactivated")

    # 2) actor still holds a confirming role and owns this proposal
    if actor_row.role not in (UserRole.manager, UserRole.admin):
        _reject(ERR_ACTOR_PERMISSION, "actor no longer has permission to confirm proposals")
    if proposal.actor_user_id != actor_row.id:
        _reject(ERR_ACTOR_PERMISSION, "actor does not own this proposal")

    # 3) action x target allowlists (revalidated against CURRENT constants)
    action_type = canonicalize(proposal.action_type)
    target_type = canonicalize(proposal.target_type)
    if action_type not in ACTION_SAFETY:
        _reject(ERR_ACTION_TARGET_ILLEGAL, f"unknown action_type '{action_type}'")
    if target_type not in TARGET_TYPES:
        _reject(ERR_TARGET_TYPE_UNKNOWN, f"unknown target_type '{target_type}'")
    if ACTION_SAFETY[action_type] != "READ" and target_type in FINANCIAL_TARGET_TYPES:
        _reject(
            ERR_ACTION_TARGET_ILLEGAL,
            f"action '{action_type}' may not target financial entity '{target_type}'",
        )

    # 4) payload still schema-valid (same rules as create; strict for
    # executable actions)
    try:
        _validate_payload(proposal.payload_json)
        validate_action_payload(action_type, proposal.payload_json)
    except ProposalValidationError as exc:
        _reject(ERR_PAYLOAD_INVALID, str(exc))

    # 5) target still exists (current existence, not the creation snapshot)
    target = _resolve_target(db, target_type, proposal.target_id)
    if target is None:
        _reject(
            ERR_TARGET_MISSING,
            f"target {target_type}:{proposal.target_id} no longer exists",
        )

    # 6) target still inside the actor's operable scope
    if not _target_in_actor_scope(actor_row, target_type, target):
        _reject(
            ERR_TARGET_OUT_OF_SCOPE,
            f"target {target_type}:{proposal.target_id} is outside the actor's scope",
        )

    # 7) business state unchanged since creation (no stale action)
    stale = _business_stale_reason(target_type, target)
    if stale is not None:
        _reject(ERR_BUSINESS_STALE, stale)


def _target_in_actor_scope(actor: User, target_type: str, target) -> bool:
    """Current operable scope of the actor over a resolved target.

    Managers/admins have full operational scope. Agents (defense in depth —
    the role check above already rejects them) may only operate on tasks
    assigned to them and settlements they own.
    """
    if actor.role in (UserRole.manager, UserRole.admin):
        return True
    if target_type == "task":
        return target.assigned_user_id == actor.id
    if target_type == "settlement":
        return target.agent_id == actor.id
    return False


def _business_stale_reason(target_type: str, target) -> str | None:
    """Business-state staleness check: refuse to execute a proposal whose
    underlying entity no longer warrants the action (e.g. an expense already
    paid/reversed, a task already completed, a lease terminated)."""
    if target_type == "task":
        if target.status != OperationalTaskStatus.PENDING:
            return f"task is no longer pending (status={target.status.value})"
        return None
    if target_type == "expense":
        if target.status != ExpenseStatus.pending:
            return f"expense is no longer pending (status={target.status.value})"
        return None
    if target_type == "income":
        if target.status != IncomeStatus.pending:
            return f"income is no longer pending (status={target.status.value})"
        return None
    if target_type == "settlement":
        if target.status != CommissionSettlementStatus.pending:
            return f"settlement is no longer pending (status={target.status.value})"
        return None
    if target_type == "lease":
        if target.status != LeaseStatus.active or target.deleted_at is not None:
            return f"lease is no longer active (status={target.status.value})"
        return None
    if target_type == "property":
        if not target.is_active:
            return "property is inactive"
        return None
    return None


def assert_executed_invariant(proposal: CopilotActionProposal) -> None:
    """Consistency contract for the future EXECUTED state (Phase C/D).

    ``status=EXECUTED`` must imply ``executed_at IS NOT NULL`` and
    ``confirmed_at`` set. Not enforced as a cross-column DB rule (a CHECK
    cannot compare columns cheaply here); asserted at the service/helper
    level so any future executor is forced through the invariant.
    """
    if proposal.status == CopilotActionStatus.EXECUTED:
        if proposal.executed_at is None:
            raise ProposalStateError("EXECUTED proposal must set executed_at")
        if proposal.confirmed_at is None:
            raise ProposalStateError("EXECUTED proposal must have confirmed_at set")


def confirm_proposal(
    db: Session, *, actor: User, proposal_id: int, now: datetime | None = None
) -> CopilotActionProposal:
    """PENDING -> CONFIRMED (idempotent replay when already CONFIRMED).

    Fail-closed confirm: in ONE DB transaction the proposal row is locked
    (``SELECT ... FOR UPDATE``) and EVERYTHING is revalidated against current
    state — actor existence/activity/permission, proposal state + expiry,
    target allowlist/existence/scope, action x target legality, payload
    schema, and business staleness. Any failure records
    ``copilot_proposal_confirm_rejected`` and raises
    ``ProposalConfirmRejectedError`` (nothing executes, proposal stays
    PENDING). Never sets ``executed_at`` and never transitions to EXECUTED
    (Phase C2).
    """
    now = now or datetime.now(timezone.utc)
    # Row lock serializes concurrent confirms/cancels/expiry so the
    # revalidation + transition are atomic w.r.t. other proposal transitions.
    proposal = (
        db.query(CopilotActionProposal)
        .filter(CopilotActionProposal.id == proposal_id)
        .with_for_update()
        .first()
    )
    if proposal is None:
        raise ProposalStateError("proposal not found")
    if proposal.status == CopilotActionStatus.CONFIRMED:
        return proposal  # replay: exactly one CONFIRMED transition ever
    if proposal.status in (
        CopilotActionStatus.EXECUTED,
        CopilotActionStatus.CANCELLED,
        CopilotActionStatus.EXPIRED,
    ):
        raise ProposalStateError(
            f"cannot confirm a {proposal.status.value} proposal"
        )
    if proposal.expires_at is not None and proposal.expires_at <= now:
        _expire_one(db, proposal, now=now)
        record_audit(
            db,
            table_name="copilot_action_proposals",
            record_id=proposal.id,
            action="copilot_proposal_confirm_rejected",
            actor_id=actor.id,
            changed_fields={
                "error_code": ERR_PROPOSAL_EXPIRED,
                "reason": "proposal has expired",
            },
        )
        raise ProposalExpiredError("proposal has expired")
    _revalidate_proposal_for_confirm(db, actor=actor, proposal=proposal)
    origin = db.get(Principal, proposal.proposed_principal_id) if proposal.proposed_principal_id else None
    current_subject = audit_context.get()[0]
    if origin is not None and origin.principal_type == PrincipalType.HUMAN and current_subject != origin.id:
        raise ProposalConfirmRejectedError(ERR_ACTOR_PERMISSION, "human proposal subject changed before confirm")

    old = serialize_row(proposal)
    result = db.execute(
        update(CopilotActionProposal)
        .where(
            CopilotActionProposal.id == proposal_id,
            CopilotActionProposal.status == CopilotActionStatus.PENDING,
        )
        .values(
            status=CopilotActionStatus.CONFIRMED,
            confirmed_at=now,
            updated_at=now,
            updated_by=actor.id,
            confirmed_principal_id=audit_context.get()[0],
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        db.expire(proposal)
        current = db.get(CopilotActionProposal, proposal_id)
        if current is not None and current.status == CopilotActionStatus.CONFIRMED:
            return current
        raise ProposalStateError("proposal was changed concurrently; refresh and retry")
    proposal.status = CopilotActionStatus.CONFIRMED
    proposal.confirmed_at = now
    proposal.updated_at = now
    record_audit(
        db,
        table_name="copilot_action_proposals",
        record_id=proposal.id,
        action="copilot_proposal_confirmed",
        actor_id=actor.id,
        changed_fields={"status": ["PENDING", "CONFIRMED"]},
        old_value=old,
        new_value=serialize_row(proposal),
    )
    return proposal


def cancel_proposal(
    db: Session, *, actor: User, proposal_id: int, now: datetime | None = None
) -> CopilotActionProposal:
    """PENDING -> CANCELLED (idempotent replay when already CANCELLED)."""
    now = now or datetime.now(timezone.utc)
    proposal = db.get(CopilotActionProposal, proposal_id)
    if proposal is None:
        raise ProposalStateError("proposal not found")
    if proposal.status == CopilotActionStatus.CANCELLED:
        return proposal
    if proposal.status in (
        CopilotActionStatus.EXECUTED,
        CopilotActionStatus.CONFIRMED,
        CopilotActionStatus.EXPIRED,
    ):
        raise ProposalStateError(
            f"cannot cancel a {proposal.status.value} proposal"
        )
    if proposal.expires_at is not None and proposal.expires_at <= now:
        _expire_one(db, proposal, now=now)
        raise ProposalExpiredError("proposal has expired")

    old = serialize_row(proposal)
    result = db.execute(
        update(CopilotActionProposal)
        .where(
            CopilotActionProposal.id == proposal_id,
            CopilotActionProposal.status == CopilotActionStatus.PENDING,
        )
        .values(
            status=CopilotActionStatus.CANCELLED,
            updated_at=now,
            updated_by=actor.id,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        db.expire(proposal)
        current = db.get(CopilotActionProposal, proposal_id)
        if current is not None and current.status == CopilotActionStatus.CANCELLED:
            return current
        raise ProposalStateError("proposal was changed concurrently; refresh and retry")
    proposal.status = CopilotActionStatus.CANCELLED
    proposal.updated_at = now
    record_audit(
        db,
        table_name="copilot_action_proposals",
        record_id=proposal.id,
        action="copilot_proposal_cancelled",
        actor_id=actor.id,
        changed_fields={"status": ["PENDING", "CANCELLED"]},
        old_value=old,
        new_value=serialize_row(proposal),
    )
    return proposal


def expire_stale_proposals(db: Session, *, now: datetime | None = None) -> int:
    """Mark PENDING proposals whose ``expires_at`` passed as EXPIRED (system).

    Not wired to the worker in Phase A+B; available as a lifecycle sweep and
    exercised by the confirm/cancel lazy-expiry path.
    """
    now = now or datetime.now(timezone.utc)
    stale = (
        db.query(CopilotActionProposal)
        .filter(
            CopilotActionProposal.status == CopilotActionStatus.PENDING,
            CopilotActionProposal.expires_at.is_not(None),
            CopilotActionProposal.expires_at <= now,
        )
        .all()
    )
    expired = 0
    for proposal in stale:
        if _expire_one(db, proposal, now=now):
            expired += 1
    return expired


def _expire_one(
    db: Session, proposal: CopilotActionProposal, *, now: datetime
) -> bool:
    old = serialize_row(proposal)
    result = db.execute(
        update(CopilotActionProposal)
        .where(
            CopilotActionProposal.id == proposal.id,
            CopilotActionProposal.status == CopilotActionStatus.PENDING,
        )
        .values(status=CopilotActionStatus.EXPIRED, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        return False
    proposal.status = CopilotActionStatus.EXPIRED
    proposal.updated_at = now
    record_audit(
        db,
        table_name="copilot_action_proposals",
        record_id=proposal.id,
        action="copilot_proposal_expired",
        actor_id=None,  # system / time-driven
        changed_fields={"status": ["PENDING", "EXPIRED"]},
        old_value=old,
        new_value=serialize_row(proposal),
    )
    return True


def _guard_execution_disabled() -> None:
    """Structural gate for the Copilot execution surface (V1.2.2 Phase C2).

    Fail closed: while ``COPILOT_EXECUTION_ENABLED`` is False (the default;
    env ``COPILOT_EXECUTION_ENABLED``) the executor refuses to run. C2
    authorizes execution ONLY when the env switch is on — the kill-switch is
    checked on every execution path.
    """
    if not COPILOT_EXECUTION_ENABLED:
        raise ExecutionDisabledError(
            "copilot action execution is disabled (COPILOT_EXECUTION_ENABLED=false)"
        )
