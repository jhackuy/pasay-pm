---
schema_version: 1
task_id: BRIDGE-ROUTER-002-HERMES-TRIAGE-001
project: pasay-pm
source: bridge-router-002-evidence
created_at: 2026-08-13T10:00:00+08:00
mode: WORKSPACE_WRITE
risk: YELLOW
type: mystery
objective: Deterministic HERMES_TRIAGE dry-run evidence card
acceptance: dry-run reports HERMES_TRIAGE with a final_route after reroute
triage_classification: {"type":"code","risk":"LOW","constraints":[]}
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

Classify this ambiguous task semantically, then re-route it deterministically.
The final execution must be a single chain and Hermes must not choose any
executor.
