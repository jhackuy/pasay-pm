# Pasay AI Development Control

Canonical workflow rules: `AI_WORKFLOW_RULES.md`

Before any execution, the worker must pass rules preflight (implemented in `scripts/wf/wf_ctl.py preflight`).

If rules are missing or hash validation fails: **FAIL CLOSED** (`BLOCKED_RULES_MISMATCH` / `BLOCKED_RULES_MISSING`), and no Max/Lily is started.

All task handoffs use the structured task envelope (`task_id`, `rules_path`, `rules_sha256`, `role`, `objective`, `allowed_paths`, `forbidden_paths`, `acceptance_criteria`). No LLM-based rule ACK loops; ACK is a machine-parsed field only.

Windows is the current canonical Pasay development authority. Pasay development, testing, commits, and GitHub pushes run from Windows by default unless the Owner explicitly changes authority again.

## Long-Term Engineering Rules

- Git authority and history safety are non-negotiable: no default-branch rewrite,
  no force push, no shared-history rewrite, no overwriting remote-only commits.
- Keep slices small: one Issue should map to one small branch/worktree, one PR,
  and one Owner acceptance step. If scope expands materially, stop and split.
- Do not expand scope beyond the current Issue. Nearby cleanup is out of scope
  unless the Issue explicitly includes it.
- Prefer targeted validation over broad expensive test runs. Use the smallest
  reliable checks that match the files and contract touched by the task.
- Never delete, skip, or xfail real failing tests just to manufacture a PASS.
- Agent self-report is never enough to claim success; independent GitHub checks,
  reviews, and human acceptance remain authoritative.
- Final Owner-facing reports default to Chinese unless the Issue explicitly says
  otherwise.

## OpenDesign Dispatcher (PASAY-OPENDESIGN-AUTO-DISPATCH-001)

The `opendesign-dispatch` GitHub workflow is the only approved entry point
for handing off `route:design-dev` Issues to OpenDesign. It is implemented
in `scripts/opendesign/` (pure stdlib) and validated by `tests/opendesign/`.
The dispatcher is event-driven, idempotent, owner-allowlisted, and never
echoes Issue / comment content into shell. Manual or scheduled polling is
NOT an approved alternative. See `docs/opendesign-dispatch.md` for the
trigger contract and PR-stage fixture validation.
