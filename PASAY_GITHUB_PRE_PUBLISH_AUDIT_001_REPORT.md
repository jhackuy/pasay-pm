# PASAY-GITHUB-PRE-PUBLISH-AUDIT-001 — GitHub 首次发布前安全审计报告

- 执行日期：2026-08-18
- 审计仓库：`D:\AI-Review\pasay-pm`
- 审计分支：`feature/telegram-ui-v2`
- 审计基线 HEAD：`8925590748f572758cdcb7bd7fa2b695757d0e7e`
- Rules preflight：`RULES_PREFLIGHT_OK`（rules_version=`2026-08-13.4`，sha256=`9ec787112a30abc6fa4890d99a934fcd24b1e8244f9866d65ce57d91e80231ef`，canonical 来源）
- 最终结论：`READY_FOR_OWNER_GITHUB_REPOSITORY_CREATION`

> 本任务未 Push、未修改 remote、未重写 Git 历史、未修改任何业务代码。

---

## 1. 当前真实 Git 状态（命令实测，非 GitHub Desktop 数字）

| 项目 | 实测值 |
| --- | --- |
| ROOT | `D:/AI-Review/pasay-pm` |
| BRANCH | `feature/telegram-ui-v2` |
| HEAD | `8925590748f572758cdcb7bd7fa2b695757d0e7e` |
| remote origin (fetch) | `macmini:/Users/jhackuy/Projects/pasay-pm` |
| remote origin (push) | `DISABLED` |
| upstream | `origin/feature/telegram-ui-v2`（merge=`refs/heads/feature/telegram-ui-v2`） |
| ahead / behind | ahead 72 / behind 0 |
| tracked 文件数 | 355 |
| 全 refs 可达 commits | 164 |
| tracked modified | 0 |
| staged | 0 |
| untracked | 572 |

`572 changed files` 与 GitHub Desktop 数字一致，但全部是 **untracked 文件**：工作树没有 tracked 修改，没有 staged 变更。

## 2. 572 个 Changes 实际分类

`git status --porcelain=v1 -uall` 实测 572 项全部为 `??` untracked，分类如下：

| 类别 | 文件数 | 判断 |
| --- | --- | --- |
| A. 正式产品源码（tracked） | 355 | 未改动，全部保留 |
| B. 正式项目文档（根目录报告 md） | 11 | 本轮纳入 Git（见 §3） |
| C. 本地运行状态/缓存/临时文件 `.build/` | 458（约 178 MB，npm/esbuild 缓存） | 不入 Git，加入 ignore |
| D. OpenDesign/MCP/Browser/Harness 产物 `.audit/` + `.research/` | 51 + 51 | 不入 Git，加入 ignore |
| E. 大型临时归档 `pv-basetmp-open-design.tar.gz` | 1（216 MB） | 不入 Git，修正 ignore 规则 |

无 tracked 临时文件，因此本轮不需要 `git rm --cached`。

## 3. `.audit` 最终结论

- `.audit/` 内容是 Penpot/浏览器 MCP 审计缓存：`pages/*.md` 页面 dump、`ux004b_*` 探针脚本、`mcp_client.py`、`run_js.py`、`.session`、`__pycache__` 等。
- `git grep` 确认没有任何 tracked 代码/文档把 `.audit/` 作为产品事实源引用；`PRODUCT_CONFORMANCE_AUDIT_001.md` 仅把 `.audit/pages/00.md…14.md` 描述为“MCP 读取产物，只读”证据。
- 结论：**本地审计缓存，不进入 Git**。本轮不删除任何文件，本地完整保留。

## 4. `.gitignore` 最小修复

修改 1 行、新增 3 行（`git diff --stat .gitignore`：6 insertions / 1 deletion）：

```gitignore
-pv-basetmp-*/
+pv-basetmp-*

+# PASAY-GITHUB-PRE-PUBLISH-AUDIT-001: local audit/build/research artifacts stay untracked
+.audit/
+.build/
+.research/
```

修复前 `pv-basetmp-*/` 只匹配目录，216 MB 的 `pv-basetmp-open-design.tar.gz` 文件漏出；改为 `pv-basetmp-*` 后文件与目录均被忽略。

## 5. Secret 审计

工具：gitleaks v8.30.1（临时下载到系统 temp，未进入项目依赖）、ripgrep 15.2.0、`git grep`。全程只记录路径/行号/类型，不记录 secret 值。

### 5.1 当前工作树

- `gitleaks dir` 全盘扫描（含 ignored 文件）：104 个命中，**全部位于被 ignore 的本地路径**，无一在 tracked 或 untracked-pushable 文件中：
  - `pv-basetmp-od-extract/`（OpenDesign 第三方源码/测试夹具）78
  - `.venv/` 6、`worktrees/` 6、`.ai-control/` 4、`.runtime/` 4、`.env` 4、`pasay-telegram-bot/.env` 2
  - 类型分布：generic-api-key 99、private-key 2、telegram-bot-api-token 2、jwt 1
- 自定义正则（Telegram Bot Token、OpenAI/DeepSeek/Anthropic API Key、GitHub Token、AWS AKIA、JWT、SSH/RSA 私钥、Postgres/Neon 连接串、Slack token）扫描“可推送文件集合”（tracked 355 + untracked 572 = 927）：**0 命中**。

```text
CURRENT_TREE_SECRET_SCAN=PASS
```

说明：根目录 `.env` 与 `pasay-telegram-bot/.env` 含真实凭据，但已被 `.gitignore` 覆盖且从未 tracked，不会被推送。建议 Owner 在首次 Push 前确认这些 key 未在其它渠道泄露过；如不放心，先 revoke/rotate 再上线。

### 5.2 Git 历史（完整可达历史）

- `gitleaks git --log-opts=--all`：160 commits 扫描，**0 leaks**，exit 0。
- 自定义正则逐 commit 扫描 `git rev-list --all` 全部 164 个 ref-reachable commits：**0 命中**。
- `git log --all -- .env`：历史上从未提交真实 `.env`；env 文件唯一新增记录是 `.env.example`。
- `git fsck --no-reflogs --unreachable` 显示存在少量不可达对象（blob/tree/commit），但首次 push 只传输 ref 可达对象，不受影响。

```text
GIT_HISTORY_SECRET_SCAN=PASS
```

无需 `git-filter-repo`，无需历史重写。

## 6. 大文件 / 二进制 / Git LFS

- 全历史 1102 个 blob，**>10 MB 的 blob：0**；最大 tracked blob 约 161 KB（`pasay-telegram-bot/pasay_bot/handlers/callback.py`）。
- tracked 二进制：仅 5 张 UX 验收截图 PNG（`ux/results/SLICE1-UX-003/shots/`，小体积，有意纳入）。
- untracked 大文件：`pv-basetmp-open-design.tar.gz` 216 MB、`.build/` 约 178 MB —— 已全部加入 ignore，不会被 push。
- Git LFS：**不需要**。

```text
GITHUB_LARGE_FILE_GATE=PASS
```

## 7. Nested repo 审计

- 唯一 Git root：`D:\AI-Review\pasay-pm\.git`。
- 嵌套 `.git` 目录仅存在于被 ignore 的 `pv-basetmp-open-design-repo/`（OpenDesign 临时 clone），不会进入 Git。
- `worktrees/` 内 5 个 `.git` 均为正式注册的 git worktree 链接文件（正常），`worktrees/` 已被 ignore。
- 结论：不会产生 submodule / nested repo 意外。

## 8. Remote 安全检查

```text
origin fetch:  macmini:/Users/jhackuy/Projects/pasay-pm
origin push:   DISABLED
branch.feature/telegram-ui-v2.remote = origin
branch.feature/telegram-ui-v2.merge  = refs/heads/feature/telegram-ui-v2
```

- 本轮只记录，未修改/未删除/未重命名 remote，未执行任何 push（且 push URL 为 `DISABLED`，意外 push 也会被拒绝）。
- 未来首次 GitHub 上线建议（下一任务执行）：旧 Mac remote 保留为 `macmini`，GitHub Private Repository 设为新的 `origin`。

## 9. 回归测试（精确数字）

| Suite | passed | failed | skipped | 其它 | exit |
| --- | --- | --- | --- | --- | --- |
| Backend（root `.venv`，`pytest tests`） | 565 | 0 | 0 | 4 deselected（`-m "not eval"`） | 0 |
| Telegram Bot（bot `.venv`，`pytest tests`） | 560 | 0 | 0 | — | 0 |

- 用时：Backend 378.20s；Bot 24.20s。
- Import smoke：`app.main` / `fastapi` / `alembic` / `sqlalchemy` OK；`pasay_bot.main` / `telegram` / `httpx` OK。
- 无 `PRE-EXISTING` 或 `INTRODUCED_BY_THIS_TASK` 失败。

## 10. 本轮 files changed

- 修改：`.gitignore`（+3 条忽略规则、1 条模式修正）
- 新增：11 个既有正式报告 md（DAILY_DIGEST_TRUTH_CLEANUP_006、PRODUCT_CONFORMANCE_AUDIT_001、REPAIR_AI_EMPLOYEE_008A 两份、TELEGRAM_OPS_REAL_WORLD_CLOSURE_005、TELEGRAM_OPS_UX_CONVERGENCE_001/003、TELEGRAM_ZERO_LEARNING_UX_004、WINDOWS_RUNTIME_SINGLETON_PERSISTENCE_007B/007C/007D_POST_REBOOT）+ 本审计报告
- 删除：无；`git rm --cached`：无；业务代码：0 处改动。

本轮提交（单次 hygiene commit）：

```text
chore: prepare repository for GitHub publishing
```

commit SHA 见 `git log -1`（本提交即包含本报告）。

## 11. 明确确认

```text
GITHUB_PUSH_PERFORMED=NO
REMOTE_CHANGED=NO
HISTORY_REWRITTEN=NO
```

## 12. 最终 Gate

| Gate | 结果 |
| --- | --- |
| A. Repository identity | `ROOT=D:\AI-Review\pasay-pm`，`BRANCH=feature/telegram-ui-v2`，`HEAD=8925590748f572758cdcb7bd7fa2b695757d0e7e`，`ORIGIN=macmini:/Users/jhackuy/Projects/pasay-pm` — PASS |
| B. Worktree hygiene | tracked modified=1（`.gitignore`），staged=0，untracked（提交后）=0，ignored=本地运行/缓存/审计目录（53 条 collapsed）— 无 Harness/audit/cache 污染提交视图 |
| C. Secret current tree | `CURRENT_TREE_SECRET_SCAN=PASS` |
| D. Secret git history | `GIT_HISTORY_SECRET_SCAN=PASS` |
| E. Large file | `GITHUB_LARGE_FILE_GATE=PASS` |
| F. Tests | Backend 565/0/0；Bot 560/0/0 |
| G. No Push | `GITHUB_PUSH_PERFORMED=NO`，`REMOTE_CHANGED=NO`，`HISTORY_REWRITTEN=NO` |

## 13. 最终状态

```text
READY_FOR_OWNER_GITHUB_REPOSITORY_CREATION
```

Owner 可创建空 Private GitHub 仓库 `pasay-pm`（不勾选 README/.gitignore/license），下一任务再配置 remotes 并执行首次 push。
