---
Name: pasay-verify
Description: Pasay targeted validation, gate, and secret-leak check
---

# /pasay-verify

Validate the current Pasay slice with the smallest reliable checks. Do not expand scope just to chase green.

## Required flow

1. Run rules preflight first:
   - `.\.venv\Scripts\python.exe scripts\wf\wf_ctl.py preflight`
2. Detect the current slice from actual Git state:
   - tracked/staged/untracked changes from `git status`
   - changed file list from `git diff --name-only`
   - if a PR already exists, you may also inspect the PR diff or merge-base
3. Reuse existing project evidence before inventing new steps:
   - `AGENTS.md`
   - `AI_WORKFLOW_RULES.md`
   - `GITHUB_DEV_WORKFLOW.md`
   - `.github/workflows/pr-ci.yml`
4. Choose targeted validation only for touched scope:
   - backend-related changes -> run the smallest relevant backend tests or import smoke
   - `pasay-telegram-bot/` changes -> run the smallest relevant bot tests or import smoke
   - workflow or governance-only changes -> validate the affected governance files and any directly related checks
5. Run any existing relevant gate for the touched scope when it is cheap and deterministic.
6. Check for obvious secret leakage in changed files only:
   - prefer installed native secret-scan capability if available
   - otherwise use a low-noise file-scoped check
   - never print secret values
7. Check for unexpected modified files outside the intended slice.

## Decision rules

- If safe targeted tests cannot be inferred, return `UNCERTAIN` instead of broadening to high-cost full-suite runs.
- Distinguish clearly:
  - real regression
  - stale or pre-existing failure
  - uncertain environment or signal
- Never modify business logic, tests, or confirmed facts just to get a passing result.

## Output

Return a short Chinese report with:

- `CHANGED_SCOPE`
- `TESTS_RUN`
- `GATES_RUN`
- `SECRET_SCAN`
- `UNEXPECTED_FILES`
- `RESULT`: `PASS` / `FAIL` / `UNCERTAIN`
- `EVIDENCE`
- `RISKS`
