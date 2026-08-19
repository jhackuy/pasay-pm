---
alwaysApply: true
---

# Pasay Governance

- Git Governance
  - Use one dedicated task branch or worktree per GitHub Issue slice.
  - Never force push, force-with-lease, rewrite shared history, overwrite remote-only commits, delete shared remote branches, bypass PR, or auto-merge.
  - Never modify authority or base-branch business code directly; all delivery goes through PR.
  - Treat Git CLI and GitHub results as authority, not IDE UI guesses.

- Task Discipline
  - One Issue maps to one narrow slice.
  - Limit edits strictly to the approved scope; no broad repo scan or unrelated refactor unless the Issue explicitly requires it.
  - Follow: locate -> implement -> targeted tests -> commit/push -> PR.
  - If blocked, scope is unclear, or safety cannot be determined, stop and report instead of endlessly exploring.

- Validation
  - After changes, run only targeted tests and existing relevant gates for the touched scope.
  - Distinguish real regression, stale test, and uncertain result explicitly.
  - Never change confirmed business facts, delete tests, skip tests, or weaken behavior just to get green.

- Reporting
  - Final owner-facing reports are in Chinese.
  - Keep code, commands, paths, SHAs, branch names, PR URLs, and field names in English.
