# Pasay AI Development Control

Canonical workflow rules: `AI_WORKFLOW_RULES.md`

Before any execution, the worker must pass rules preflight (implemented in `scripts/wf/wf_ctl.py preflight`).

If rules are missing or hash validation fails: **FAIL CLOSED** (`BLOCKED_RULES_MISMATCH` / `BLOCKED_RULES_MISSING`), and no Max/Lily is started.

All task handoffs use the structured task envelope (`task_id`, `rules_path`, `rules_sha256`, `role`, `objective`, `allowed_paths`, `forbidden_paths`, `acceptance_criteria`). No LLM-based rule ACK loops; ACK is a machine-parsed field only.

Windows is the current canonical Pasay development authority. Pasay development, testing, commits, and GitHub pushes run from Windows by default unless the Owner explicitly changes authority again.
