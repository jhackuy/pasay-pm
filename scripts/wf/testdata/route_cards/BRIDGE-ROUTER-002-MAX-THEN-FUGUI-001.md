---
schema_version: 1
task_id: BRIDGE-ROUTER-002-MAX-THEN-FUGUI-001
project: pasay-pm
source: bridge-router-002-evidence
created_at: 2026-08-13T10:00:00+08:00
mode: WORKSPACE_WRITE
risk: YELLOW
type: bot_ux
objective: Deterministic MAX_THEN_FUGUI_ACCEPTANCE dry-run evidence card
acceptance: dry-run reports MAX_THEN_FUGUI_ACCEPTANCE with acceptance_target=FUGUI
real_device_test: true
requested_capabilities:
  - workspace_write
  - run_tests
expected_branch: feature/telegram-ui-v2
expected_head: 112d3325b4af036a73c5d510e7fd81eb8d0ec39f
allow_code_change: true
allow_commit: true
allow_migration: false
allow_production_deploy: false
allow_production_db_write: false
---

Apply a bot UX polish change and verify the interactive Telegram menu on a real
device after implementation. Max executes the code; Fugui only performs the
acceptance check.
