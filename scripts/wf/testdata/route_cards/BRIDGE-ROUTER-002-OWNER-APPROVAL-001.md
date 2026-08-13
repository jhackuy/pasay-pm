---
schema_version: 1
task_id: BRIDGE-ROUTER-002-OWNER-APPROVAL-001
project: pasay-pm
source: bridge-router-002-evidence
created_at: 2026-08-13T10:00:00+08:00
mode: WORKSPACE_WRITE
risk: YELLOW
type: code
objective: Deterministic OWNER_APPROVAL_REQUIRED dry-run evidence card
acceptance: dry-run reports OWNER_APPROVAL_REQUIRED with 0/0 starts
manual_approval: true
requested_capabilities:
  - workspace_write
expected_branch: feature/telegram-ui-v2
expected_head: 112d3325b4af036a73c5d510e7fd81eb8d0ec39f
allow_code_change: true
allow_commit: true
allow_migration: false
allow_production_deploy: false
allow_production_db_write: false
---

Wait for Owner approval before applying this manual approval action. No
executor or planner may start until the approval is granted.
