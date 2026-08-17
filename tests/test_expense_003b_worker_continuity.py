"""PASAY-VNEXT-EXPENSE-OPERATION-003B — worker continuity + repair-link tests.

E11 — 30 worker ticks on an approved-but-unpaid expense keep exactly ONE active
      PAYMENT_PENDING task (no duplicates, no 30 Telegram sends).
E12 — fully-verified payment on the next tick stops payment chasing (any stale
      PAYMENT_PENDING task is completed).
E13 — (covered in test_expense_003b_payment_truth.py).
E14 — a Repair-linked Expense being PAID does NOT close the Repair (008A gate
      stays intact; only real verification closes a Repair).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.financial import Expense, ExpenseStatus
from app.models.operations import (
    NotificationOutbox,
    NotificationStatus,
    OperationalTask,
    OperationalTaskStatus,
    OperationalTaskType,
)
from app.models.repair import (
    RepairOperation,
    RepairOperationStatus,
    RepairProposal,
    RepairProposalStatus,
)
from app.services.expense_claims import create_claim, verify_claim
from app.services.expense_payment_truth import payment_truth
from app.services.operations.generation import generate_business_tasks
from app.services.operations.reconcile import reconcile_tasks
from app.services.repairs import payment as repair_payment

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _user(db, username, role):
    from app.models.user import User

    u = User(username=username, role=role,
             api_key_hash=__import__("secrets").token_urlsafe(24), is_active=True)
    db.add(u)
    db.flush()
    return u


def _approved_expense(db, *, amount="28000.00", approved_days_ago=10, actor_id=1):
    e = Expense(expense_date=date(2026, 8, 1), category="维修", amount=amount,
                payee="Fix-It Co", description="aircon", status=ExpenseStatus.approved,
                approved_by=actor_id,
                approved_at=NOW - timedelta(days=approved_days_ago))
    db.add(e)
    db.flush()
    return e


@pytest.fixture()
def assignee(db_session, monkeypatch):
    from app.services.operations import generation, config

    u = _user(db_session, "secretary", "manager")
    monkeypatch.setattr(generation, "DEFAULT_ASSIGNED_USER_ID", u.id)
    monkeypatch.setattr(generation, "SECRETARY_ASSIGNEE_ID", u.id)
    monkeypatch.setattr(config, "DEFAULT_ASSIGNED_USER_ID", u.id)
    return u


# E11 — 30 worker ticks produce exactly ONE active payment task and no spam
def test_e11_30_ticks_single_payment_task(assignee, db_session):
    expense = _approved_expense(db_session)
    db_session.commit()

    all_created = 0
    for _ in range(30):
        ncreated, notif = generate_business_tasks(db_session, now=NOW)
        all_created += ncreated
        reconcile_tasks(db_session, now=NOW)
        db_session.commit()

    active = (
        db_session.query(OperationalTask)
        .filter(
            OperationalTask.source_type == "expense",
            OperationalTask.source_id == expense.id,
            OperationalTask.task_type == OperationalTaskType.PAYMENT_PENDING,
            OperationalTask.status.in_([
                OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS,
            ]),
        )
        .all()
    )
    assert len(active) == 1  # exactly one active payment task (E11)
    assert active[0].status == OperationalTaskStatus.PENDING

    # No 30 notifications sent: same-day daily dedupe caps proactive sends.
    outbox = (
        db_session.query(NotificationOutbox)
        .filter(NotificationOutbox.task_id == active[0].id)
        .filter(NotificationOutbox.status == NotificationStatus.SENT)
        .count()
    )
    assert outbox <= 1  # at most one reminder per PH day (no per-tick spam)


# E12 — verified full payment stops payment chasing on the next tick
def test_e12_verified_full_paid_stops_chasing(assignee, db_session):
    expense = _approved_expense(db_session)
    db_session.commit()
    generate_business_tasks(db_session, now=NOW)
    db_session.commit()

    # Secretary reports AND Owner verifies full 28,000 in one claim.
    claim, _ = create_claim(db_session, expense, claimed_amount="28000.00",
                            claimed_by=assignee.id, evidence_ids=[])
    verify_claim(db_session, expense, claim.id, verified_by=1)
    db_session.commit()
    assert payment_truth(db_session, expense).fully_paid is True

    # Next tick: the stale PAYMENT_PENDING task must be completed (no more chase).
    for _ in range(3):
        generate_business_tasks(db_session, now=NOW)
        reconcile_tasks(db_session, now=NOW)
        db_session.commit()

    after = (
        db_session.query(OperationalTask)
        .filter(
            OperationalTask.source_type == "expense",
            OperationalTask.source_id == expense.id,
            OperationalTask.task_type == OperationalTaskType.PAYMENT_PENDING,
        )
        .all()
    )
    truth = payment_truth(db_session, expense)
    assert truth.fully_paid is True
    assert all(t.status == OperationalTaskStatus.COMPLETED for t in after)


# E12b — partial verified (10k) keeps exactly one follow-up task with remaining
def test_e12b_partial_keeps_single_followup(assignee, db_session):
    expense = _approved_expense(db_session)
    db_session.commit()
    generate_business_tasks(db_session, now=NOW)
    db_session.commit()

    claim, _ = create_claim(db_session, expense, claimed_amount="10000.00",
                            claimed_by=assignee.id)
    verify_claim(db_session, expense, claim.id, verified_by=1)
    db_session.commit()

    for _ in range(30):
        generate_business_tasks(db_session, now=NOW)
        reconcile_tasks(db_session, now=NOW)
        db_session.commit()

    truth = payment_truth(db_session, expense)
    assert truth.verified_paid == 10000
    assert truth.remaining == 18000  # stays exactly 18k (§4 / §13 Scenario B)
    active = (
        db_session.query(OperationalTask)
        .filter(
            OperationalTask.source_type == "expense",
            OperationalTask.source_id == expense.id,
            OperationalTask.task_type == OperationalTaskType.PAYMENT_PENDING,
            OperationalTask.status.in_([
                OperationalTaskStatus.PENDING, OperationalTaskStatus.IN_PROGRESS,
            ]),
        )
        .all()
    )
    assert len(active) == 1  # exactly one active follow-up (Scenario B)


# E14 — Repair-linked Expense PAID does not close the Repair
def test_e14_repair_not_closed_by_expense_paid(db_session):
    repair = RepairOperation(issue="aircon broken", status=RepairOperationStatus.WAITING_PAYMENT,
                             next_action="awaiting payment", created_by=1)
    db_session.add(repair)
    db_session.flush()

    expense = Expense(expense_date=date(2026, 8, 1), category="维修", amount="28000.00",
                      payee="Fix-It Co", status=ExpenseStatus.approved,
                      approved_by=1, approved_at=NOW - timedelta(days=2))
    db_session.add(expense)
    db_session.flush()

    proposal = RepairProposal(repair_id=repair.id, version=1, vendor="Fix-It Co",
                              amount="28000.00", status=RepairProposalStatus.APPROVED,
                              expense_id=expense.id)
    db_session.add(proposal)
    db_session.flush()
    db_session.commit()

    # Mark the expense fully paid via a verified claim.
    claim, _ = create_claim(db_session, expense, claimed_amount="28000.00", claimed_by=1)
    verify_claim(db_session, expense, claim.id, verified_by=1)
    repair_payment.on_expense_paid(db_session, repair)  # 008A coordination hook
    db_session.commit()

    assert expense.status == ExpenseStatus.paid
    assert repair.status == RepairOperationStatus.VERIFYING  # at most VERIFYING
    assert repair.status != RepairOperationStatus.CLOSED  # NEVER closed
    assert repair.closed_at is None  # real verification has NOT happened
