---
Name: pasay-preflight
Description: Pasay repo, branch, dirty-tree, and remote preflight (SOLO Mode)
---

# /pasay-preflight (SOLO Mode)

在实现或交付前快速确认 Git 状态。不再执行 `wf_ctl.py preflight` 与 rules hash 校验。

## Required Flow

1. Read and report:
   - current repo path
   - current branch
   - current HEAD SHA
   - `git status --short --branch`
   - `git remote -v`
2. Fetch latest remote state:
   - `git fetch --all --prune`
3. Determine whether the current branch is suitable for the current Milestone:
   - task/milestone branch is preferred
   - authority / base branch 不适合直接实现
   - unexpected dirty changes 必须视为 blocker 或 risk
4. 不修改代码、不 commit、不 push、不 merge。

## Decision Rules

- Prefer Git CLI and GitHub results over IDE UI guesses.
- If there are unrelated dirty changes, branch ambiguity, missing remote sync, or safety cannot be determined, stop and report.
- Never use force push, force-with-lease, branch deletion, history rewrite, or any destructive cleanup.

## Output

Return a short Chinese report with:

- `REPO`, `BRANCH`, `HEAD`, `REMOTE`
- `DIRTY_TREE`: `CLEAN` / `DIRTY`
- `BRANCH_SUITABILITY`: `PASS` / `FAIL` / `UNCERTAIN`
- `PREFLIGHT_RESULT`: `PASS` / `FAIL` / `UNCERTAIN`
- `RISKS`
