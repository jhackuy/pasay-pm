---
Name: pasay-verify
Description: Pasay targeted validation, gate, and secret-leak check (SOLO Mode)
---

# /pasay-verify (SOLO Mode)

以最小可靠检查验证当前 Milestone / Slice。不为追绿而扩大范围。

## Required Flow

1. 从 Git state 检测当前改动范围：
   - `git status` tracked/staged/untracked
   - `git diff --name-only` changed files
   - 若已有 PR，可参考 PR diff / merge-base
2. 复用已有项目证据：
   - `AGENTS.md`, `project_rules.md`, `SOLO_HANDOFF.md`, `CURRENT_ARCHITECTURE.md`
   - `.github/workflows/pr-ci.yml`
3. 仅对受影响范围执行 targeted validation：
   - backend 改动 → 最小相关 backend tests 或 import smoke
   - `pasay-telegram-bot/` 改动 → 最小相关 bot tests 或 import smoke
   - cloudflare-worker 改动 → `npx tsc` + tsx spec runner + targeted worker tests
   - governance/rules only 改动 → 校验目标文件并执行最直接的相关检查
   - migrations 改动 → upgrade/downgrade 双通审计；downgrade 必须 sa.inspect 列属性门控防止语义丢失
4. 成本低且确定性的相关 Gate 顺便执行。
5. Secret leakage 只在 changed files 范围内检查：
   - 优先使用 IDE 原生 secret-scan
   - 否则低噪声文件级扫描；永远不打印明文
6. 检查范围外的意外改动。

## Decision Rules

- 如果安全 targeted tests 无法推断，返回 `UNCERTAIN`，不要扩大到昂贵的全量。
- 明确区分：真实回归 / 历史遗留失败 / 不确定环境或信号。
- 永远不为 PASS 修改业务逻辑、测试或已确认事实。

## Output

Short Chinese report with:

- `CHANGED_SCOPE`, `TESTS_RUN`, `GATES_RUN`
- `SECRET_SCAN`: `PASS` / `FAIL` / `NOT_RUN`
- `UNEXPECTED_FILES`: list / `NONE`
- `RESULT`: `PASS` / `FAIL` / `UNCERTAIN`
- `EVIDENCE` + `RISKS`
