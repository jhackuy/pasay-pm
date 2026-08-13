# Pasay AI Development Control

Canonical workflow rules: `/Users/jhackuy/Projects/pasay-pm/AI_WORKFLOW_RULES.md`

Before any execution, the worker must pass rules preflight (implemented in `scripts/wf/wf_ctl.py preflight`).

If rules are missing or hash validation fails: **FAIL CLOSED** (`BLOCKED_RULES_MISMATCH` / `BLOCKED_RULES_MISSING`), and no Max/Lily is started.

All task handoffs use the structured task envelope (`task_id`, `rules_path`, `rules_sha256`, `role`, `objective`, `allowed_paths`, `forbidden_paths`, `acceptance_criteria`). No LLM-based rule ACK loops; ACK is a machine-parsed field only.

Windows copies of the rules are read-only mirrors of the Mac canonical file, never an authority.
