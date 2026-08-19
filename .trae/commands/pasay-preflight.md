---
Name: pasay-preflight
Description: Pasay repo, branch, dirty-tree, and remote preflight
---

# /pasay-preflight

Run a deterministic preflight for the current Pasay task without modifying business code.

## Required flow

1. From repo root, run rules preflight first:
   - `.\.venv\Scripts\python.exe scripts\wf\wf_ctl.py preflight`
2. Read and report:
   - current repo path
   - current branch
   - current HEAD SHA
   - `git status --short --branch`
   - `git remote -v`
3. Fetch latest remote state:
   - `git fetch --all --prune`
4. Determine whether the current branch is suitable for the current GitHub Issue slice:
   - task branch or task worktree is preferred
   - base or authority branch is not suitable for direct implementation
   - unexpected dirty changes must be treated as a blocker or risk
5. Do not modify code, do not commit, do not push, do not merge.

## Decision rules

- Prefer Git CLI and GitHub results over IDE UI guesses.
- If there are unrelated dirty changes, branch ambiguity, missing remote sync, or safety cannot be determined, stop and report.
- Never use force push, force-with-lease, branch deletion, history rewrite, or any destructive cleanup.

## Output

Return a short Chinese report with:

- `RULES_PREFLIGHT`: `OK` or `FAIL`
- `REPO`
- `BRANCH`
- `HEAD`
- `REMOTE`
- `DIRTY_TREE`: `CLEAN` / `DIRTY`
- `BRANCH_SUITABILITY`: `PASS` / `FAIL` / `UNCERTAIN`
- `PREFLIGHT_RESULT`: `PASS` / `FAIL` / `UNCERTAIN`
- `RISKS`
