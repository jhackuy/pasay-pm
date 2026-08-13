---
schema_version: 1
task_id: BRIDGE-ROUTER-002-HERMES-THEN-MAX-001
project: pasay-pm
source: bridge-router-002-evidence
created_at: 2026-08-13T10:00:00+08:00
mode: WORKSPACE_WRITE
risk: YELLOW
type: code
objective: Deterministic HERMES_THEN_MAX dry-run evidence card
acceptance: dry-run reports HERMES_THEN_MAX with hermes_started=1 and max_started=1
constraints:
  - architecture_change
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

Refactor the workflow routing layer with an architecture-level change:
introduce a pure dispatch controller and wire it into the runner with unit
tests. Hermes plans first, then Max executes the change.
