---
Name: pasay-closeout
Description: Pasay diff, verification, commit, push, PR, and CI closeout
---

# /pasay-closeout

Close out the current Pasay slice after implementation and verification are complete. Never merge.

## Preconditions

- The current task or Issue explicitly allows commit and push.
- Verification evidence already exists, or you run `/pasay-verify` first.

## Required flow

1. Run rules preflight first:
   - `.\.venv\Scripts\python.exe scripts\wf\wf_ctl.py preflight`
2. Inspect and report final Git state:
   - current branch
   - current HEAD SHA
   - final diff summary
   - dirty tree status
3. Confirm branch safety:
   - do not close out directly from authority or base branch
   - task branch or task worktree branch is expected
4. Confirm verification status:
   - targeted tests run
   - relevant gate or CI evidence collected
   - unresolved failures or uncertainty must be reported before any write action
5. If and only if the task explicitly allows it:
   - create a commit
   - push the current task branch
   - create a PR if none exists, or update the existing PR
6. Read CI or check status for the current PR after push.
7. Stop after reporting. Never merge. Never auto-merge.

## Hard bans

- No force push
- No force-with-lease
- No history rewrite
- No branch deletion
- No secret mutation
- No branch-protection mutation
- No automatic merge

## Output

Return a Chinese final closeout report with:

- `BRANCH`
- `HEAD`
- `FILES_CHANGED`
- `VERIFY_STATUS`
- `COMMIT`
- `PUSH`
- `PR`
- `CI_STATUS`
- `FINAL_RESULT`: `PASS` / `FAIL` / `UNCERTAIN`
- `RISKS`
