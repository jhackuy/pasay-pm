"""Configuration constants for V1.2 task generation / notification policy.

Every task-type window and notification policy lives here (or in a
rule's metadata) so nothing is hard-coded across the codebase.
"""
from __future__ import annotations

import os

# Business-source windows (days).
LEASE_EXPIRY_WINDOW_DAYS = 30
RENT_DUE_ADVANCE_DAYS = 3
APPROVAL_PENDING_AFTER_DAYS = 2
PAYMENT_PENDING_AFTER_DAYS = 1
SETTLEMENT_PENDING_AFTER_DAYS = 1

# Fallback assignee for business-source tasks with no explicit owner (the
# admin owner by default) so proactive notifications get a real recipient.
DEFAULT_ASSIGNED_USER_ID = int(os.getenv("OPERATIONS_DEFAULT_ASSIGNEE", "1"))

# Designated Secretary/Operator (human) identity: the deterministic default
# for C2 "安排秘书跟进" / secretary-role followup/assignment when there is no
# unique active agent candidate. Human operator channel, NOT the AI agent and
# NOT a legacy identity. Env-tunable override: OPERATIONS_SECRETARY_ASSIGNEE.
SECRETARY_ASSIGNEE_ID = int(os.getenv("OPERATIONS_SECRETARY_ASSIGNEE", "14"))

# Recurring-rule periods.
QUARTERLY_MONTHS = 3
YEARLY_MONTHS = 12
DEFAULT_FIXED_INTERVAL_MONTHS = 1

# Notification policy (exponential backoff).
NOTIFY_CHANNEL_TELEGRAM = "telegram"
NOTIFY_MAX_ATTEMPTS = 5
NOTIFY_BACKOFF_BASE_SECONDS = 30
# Claim lease: a notifier claim (claimed_at) is only re-claimable after this
# many seconds — a worker that crashes mid-send is retried after the lease
# expires (at-least-once), while a live claim is never double-claimed.
NOTIFY_CLAIM_LEASE_SECONDS = 300

# Worker / scheduler batch sizes.
SCHEDULER_RULE_BATCH = 20
OUTBOX_CLAIM_BATCH = 10
SNOOZE_REDELIVERY_BATCH = 20

# Snooze redelivery outbox dedupe keys: ``snooze-redelivery:{task_id}:{generation}:{window}``.
# The generation (bumped on every snooze / complete / cancel) plus the window
# (the exact ``snoozed_until`` value) make the key valid for one logical
# reminder only, so a re-snooze produces a fresh key and a DROPPED old
# generation can never block a new generation for the same window.
SNOOZE_REDELIVERY_KEY_PREFIX = "snooze-redelivery:"
