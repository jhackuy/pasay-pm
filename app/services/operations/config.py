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

# Recurring-rule periods.
QUARTERLY_MONTHS = 3
YEARLY_MONTHS = 12
DEFAULT_FIXED_INTERVAL_MONTHS = 1

# Notification policy (exponential backoff).
NOTIFY_CHANNEL_TELEGRAM = "telegram"
NOTIFY_MAX_ATTEMPTS = 5
NOTIFY_BACKOFF_BASE_SECONDS = 30

# Worker / scheduler batch sizes.
SCHEDULER_RULE_BATCH = 20
OUTBOX_CLAIM_BATCH = 10
