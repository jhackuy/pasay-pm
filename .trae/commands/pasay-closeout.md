---
Name: pasay-closeout
Description: Pasay diff, verification, commit, push, PR closeout (SOLO Mode)
---

# /pasay-closeout (SOLO Mode)

实现与验证完成后交付当前 Milestone / Branch。Never merge.

## Preconditions

- 当前任务或 Milestone 明确允许 commit & push
- Verification evidence 已存在，或先跑 `/pasay-verify`

## Required Flow

1. Inspect and report final Git state:
   - current branch, HEAD SHA, final diff summary, dirty tree status
2. Confirm branch safety:
   - 不从 authority/base 分支直接 closeout；task/milestone 分支才交付
3. Confirm verification status:
   - targeted tests 已运行
   - 相关 gate / CI 证据已收集
   - 未解决失败或不确定性必须在任何写动作前报告
4. 仅当任务明确允许时：
   - create commit（Conventional Commits 推荐）
   - push 当前分支到 origin
   - 无 PR 时创建 PR，已有 PR 时 push 更新即可
5. 停止并报告。永不 merge。永不 auto-merge。

## Hard Bans

- No force push / force-with-lease
- No history rewrite / shared branch deletion
- No secret mutation / branch-protection mutation
- No automatic merge
- No production deploy

## Output

Chinese final closeout report with:

- `BRANCH`, `HEAD`, `FILES_CHANGED`
- `VERIFY_STATUS`
- `COMMIT` / `PUSH` / `PR`（含编号与链接）
- `CI_STATUS`（若可获得）
- `FINAL_RESULT`: `PASS` / `FAIL` / `UNCERTAIN`
- `RISKS`
