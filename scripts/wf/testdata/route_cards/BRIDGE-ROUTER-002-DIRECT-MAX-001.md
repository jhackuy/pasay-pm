---
schema_version: 1
task_id: BRIDGE-ROUTER-002-DIRECT-MAX-001
project: pasay-pm
source: bridge-router-002-evidence
created_at: 2026-08-13T10:00:00+08:00
mode: READ_ONLY
risk: GREEN
type: code
objective: Deterministic DIRECT_MAX dry-run evidence card
acceptance: dry-run reports DIRECT_MAX with max_started=1
requested_capabilities:
  - repo_read
  - git_status
expected_branch: feature/telegram-ui-v2
expected_head: 112d3325b4af036a73c5d510e7fd81eb8d0ec39f
allow_code_change: false
allow_commit: false
allow_migration: false
allow_production_deploy: false
allow_production_db_write: false
---

Implement a small pure-python helper that normalizes list inputs. Add focused
unit tests and run them locally. Do not touch any other area of the codebase.
