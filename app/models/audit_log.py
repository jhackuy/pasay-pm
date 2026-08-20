from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import AuditMixin, Base, pg_enum


class AuditAction(str, Enum):
    # V1.1 values (never reorder / rename — old audit rows reference them).
    create = "create"
    update = "update"
    soft_delete = "soft_delete"
    confirm = "confirm"
    approve = "approve"
    reject = "reject"
    pay = "pay"
    reverse = "reverse"
    # V1.2 PROACTIVE OPERATIONS values (appended only).
    task_created = "task_created"
    task_completed = "task_completed"
    task_cancelled = "task_cancelled"
    task_snoozed = "task_snoozed"
    rule_created = "rule_created"
    rule_updated = "rule_updated"
    rule_disabled = "rule_disabled"
    task_auto_completed = "task_auto_completed"
    task_auto_cancelled = "task_auto_cancelled"
    task_backfilled = "task_backfilled"
    # V1.2.2 OPS COPILOT values (appended only).
    task_reminder_redelivered = "task_reminder_redelivered"
    outbox_dropped = "outbox_dropped"
    copilot_context_built = "copilot_context_built"
    copilot_proposal_created = "copilot_proposal_created"
    copilot_proposal_confirmed = "copilot_proposal_confirmed"
    copilot_proposal_cancelled = "copilot_proposal_cancelled"
    copilot_proposal_expired = "copilot_proposal_expired"
    copilot_proposal_confirm_rejected = "copilot_proposal_confirm_rejected"
    # V1.2.2 Phase C2 (appended only).
    copilot_proposal_executing = "copilot_proposal_executing"
    copilot_proposal_executed = "copilot_proposal_executed"
    copilot_proposal_execution_rejected = "copilot_proposal_execution_rejected"
    task_reassigned = "task_reassigned"
    # PASAY-V2-FOUNDATION-001 (appended only).
    task_updated = "task_updated"
    task_completed_via_approval = "task_completed_via_approval"
    task_completed_via_rejection = "task_completed_via_rejection"
    task_completed_via_payment = "task_completed_via_payment"
    # AI-OPS-FOUNDATION-001 (appended only): a reminder refreshed the SAME
    # active task (one business issue = one active task) instead of creating a
    # duplicate sibling.
    task_reminded = "task_reminded"
    # AI-OPS-FOUNDATION-001 §8: an unresolved promise/follow-up escalated to
    # the Owner per policy (deterministic, system-triggered).
    task_escalated = "task_escalated"
    # TELEGRAM-OPS-UX-CONVERGENCE-003 §1.5: the human tapped ✅ Acknowledge on
    # a proactive reminder (PENDING -> IN_PROGRESS, reminders stop for today).
    task_acknowledged = "task_acknowledged"
    # PASAY-AI-EMPLOYEE-FOUNDATION-007 (appended only): AI Employee Foundation
    # audit actions — each distinguishes exactly WHO did WHAT (a human-supplied
    # low-risk write vs a self-healing resume vs a payment-promise scheduler).
    phone_direct_update = "phone_direct_update"
    blocked_created = "blocked_created"
    blocked_resolved = "blocked_resolved"
    payment_promise_recorded = "payment_promise_recorded"
    promise_fulfilled = "promise_fulfilled"
    promise_missed_refollow = "promise_missed_refollow"
    action_assigned = "action_assigned"
    rent_followup_sent = "rent_followup_sent"
    # REPAIR-AI-EMPLOYEE-WORKFLOW-008A (appended only): Repair Operation
    # lifecycle audit actions (operation / versioned proposal / verification).
    repair_created = "repair_created"
    proposal_submitted = "proposal_submitted"
    proposal_approved = "proposal_approved"
    proposal_rejected = "proposal_rejected"
    repair_completed_pending_verification = "repair_completed_pending_verification"
    repair_closed_after_verification = "repair_closed_after_verification"
    repair_cancelled = "repair_cancelled"
    # PASAY-EXPENSE-OPERATION-003B (appended only): expense payment-claim truth
    # lifecycle. Each distinguishes exactly WHO did WHAT (actor_id real human /
    # principal; never mixes SYSTEM/AI/Owner/Secretary into one).
    expense_claim_created = "expense_claim_created"
    expense_claim_verified = "expense_claim_verified"
    expense_claim_failed = "expense_claim_failed"
    expense_claim_reversed = "expense_claim_reversed"
    expense_amount_mismatch = "expense_amount_mismatch"
    expense_partially_paid = "expense_partially_paid"
    expense_fully_paid = "expense_fully_paid"
    expense_requires_reapproval = "expense_requires_reapproval"
    expense_resubmitted = "expense_resubmitted"
    expense_rejected = "expense_rejected"  # explicit rejection with reason + version preserved
    # PASAY-TASK-002 (appended only; never rename or reorder prior values):
    # Membership/organization lifecycle audit actions.
    org_created = "org_created"
    org_first_owner_activated = "org_first_owner_activated"
    secretary_invited = "secretary_invited"
    secretary_invite_accepted = "secretary_invite_accepted"
    secretary_invite_cancelled = "secretary_invite_cancelled"
    secretary_removed = "secretary_removed"


class AuditLog(AuditMixin, Base):
    __tablename__ = "audit_logs"

    table_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    record_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[AuditAction] = mapped_column(
        pg_enum(AuditAction, "audit_action"), nullable=False
    )
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subject_principal_id: Mapped[int | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    caller_principal_id: Mapped[int | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    credential_id: Mapped[int | None] = mapped_column(ForeignKey("api_credentials.id"), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(30), nullable=True)
    changed_fields: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
