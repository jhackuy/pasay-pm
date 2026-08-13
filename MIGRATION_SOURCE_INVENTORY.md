# WIN-SINGLE-NODE-MIGRATION-001 — Source Inventory

记录时间：2026-08-14 00:1x (Asia/Shanghai)
记录方式：SSH 只读盘点 + 本地 git 检查。全程未输出任何 secret 明文。
Preflight：`RULES_PREFLIGHT_OK`（AI_WORKFLOW_RULES.md v2026-08-13.4，SHA256 9ec787112a30abc6fa4890d99a934fcd24b1e8244f9866d65ce57d91e80231ef）

---

## 1. Mac 源（冻结与盘点）

| 项 | 值 |
|---|---|
| 主机 | Mac mini (Darwin arm64, macOS 26.6.1, 192.168.50.168, 用户 jhackuy) |
| canonical repo | `/Users/jhackuy/Projects/pasay-pm` |
| 另一源码副本 | `/Users/jhackuy/Documents/Codex/pasay-pm`（与 Projects 同 HEAD，git clean） |
| 运行时部署 | `/opt/pasay-pm`（launchd 服务，回滚用） |
| branch | `feature/telegram-ui-v2` |
| HEAD | `2bc4c7c65b11c3d0e5e1e4840a9f4760757763f4` |
| git status | **CLEAN**（无未提交修改，无需 migration safety commit） |
| 最近提交 | 2bc4c7c WF-GUARDRAILS-CANONICAL-SYNC-001；6688340 SLICE3-UX-PERSISTENT-MENU-002；5e1b955 BRIDGE-ROUTER-002 |

### Mac Hermes（v0.20.0, Python 3.11.15）

- 安装目录：`/Users/jhackuy/.hermes/hermes-agent`
- HERMES_HOME：`/Users/jhackuy/.hermes`
- Pasay profile：`/Users/jhackuy/.hermes/profiles/pasay-property`（property gateway，当前未运行）
- SOUL.md：`~/.hermes/SOUL.md`（orchestrator 工作模式指针）
- Playbook：`~/.hermes/AGENT_PLAYBOOK.md`（multi-agent product engineering 唯一权威）
- config：`~/.hermes/config.yaml`（deepseek-v4-flash）+ profile `config.yaml`（deepseek + property 插件 + telegram allow_from）
- memories：`~/.hermes/memories/MEMORY.md`、`USER.md`（Pasay 架构/口径/操作纪律，均为 Pasay 相关有效上下文）
- plans：`~/.hermes/plans/rental-management.md`（项目章程）、`rental-management-phase1-bot-audit.md`、`agent-playbook-design-proposal.md`
- skills（Pasay 相关）：
  - `productivity/property-management`（SKILL.md + scripts/property_client.py + assets/config.env + roles.json）
  - `productivity/pasay-pm-data-model`（SKILL.md + references + scripts/pasay_pm_pgdump.py）
  - `software-development/telegram-bot-engineering`（含 pasay-native-bot-case-study.md）
  - `autonomous-ai-agents/multi-agent-product-engineering`（playbook loader）
  - `autonomous-ai-agents/codex`（Codex CLI 调用技能）
  - `autonomous-ai-agents/hermes-gateway-operations`
- cron：主 profile `e61bca47e92c`（PASay 每日异常摘要，paused）；property profile `e61bca47e92c_property`（scheduled，0 8 * * *，投递 telegram:-1004433994558）
- plugins：`profiles/pasay-property/plugins/property`（property 工具集）
- scripts：`~/.hermes/scripts/pasay_nas_backup.sh`（NAS 备份，Mac 专属，不在本次迁移范围）
- launchd：ai.hermes.gateway / ai.hermes.property-gateway / ai.hermes.app / ai.pasay.api / ai.pasay.control-runner / ai.pasay.operations-worker / ai.pasay.postgres / ai.pasay.telegram-bot / com.ai-controller.*
- 当前进程：Hermes app(38524) + gateway(38617) + serve(38578)；uvicorn(53953)；postgres；operations-worker(53968)；**control-runner(1078/732, ai.pasay.control-runner 运行中)**

### Mac Codex

- 安装：ChatGPT.app 捆绑 0.147 + `~/.local/bin/codex`
- config：`~/.codex/config.toml`（custom provider deepseek，base_url api.deepseek.com，wire_api responses，model deepseek-v4-flash；projects 信任 `/Users/jhackuy/Projects/pasay-pm`）
- 用户技能：无（仅 .system 内置）
- Mac 无运行中的 codex 进程

### Mac .ai-control

- 仅 `results/WF-004`、`results/WF-005`（历史测试证据）与 `state/wf003`；无新增价值内容。Windows .ai-control 已更全，不回迁。

### Mac 未提交代码

无（CLEAN）。无需 safety commit；git 对象与 Windows origin ref 保留完整可回滚。

---

## 2. Windows 本机现状（迁移目标）

| 项 | 值 |
|---|---|
| repo | `D:\AI-Review\pasay-pm` |
| branch | `feature/telegram-ui-v2` |
| HEAD | `9d63a3ebffcbb7c79c6fd073759128c46acfc807` |
| 工作区 | 22 个 tracked 文件修改（Bot V1 WIP，+1733/-248），50 个 untracked 条目（新 bot 模块/测试、runtime 证据、worktrees） |
| origin | `macmini:/Users/jhackuy/Projects/pasay-pm`（fetch），push DISABLED |
| Windows Hermes | v0.20.0，HERMES_HOME=`C:\Users\Admin\AppData\Local\hermes`（config.yaml deepseek-v4-flash + fallback alibaba；SOUL.md 为通用执行纪律；memories 已有 Windows 本机知识，无 Pasay 上下文；skills 已有 codex/productivity 基础目录，缺 property-management/pasay-pm-data-model/telegram-bot-engineering；plugins/plans 为空） |
| Windows Codex CLI | `C:\Users\Admin\AppData\Local\OpenAI\Codex\bin\8e8bf206e63ac436\codex.exe`（codex-cli 0.147.0-alpha.6.6，可执行）；PATH 里的 WindowsApps `codex.exe` 无法直接调用（Access denied） |
| Codex config | `C:\Users\Admin\.codex\config.toml`（custom deepseek v4-flash，sandbox elevated，plugins enabled，auth.json 存在） |
| 后端 Runtime | `http://127.0.0.1:8001/health` → `{"status":"ok"}`（2 个 uvicorn 实例，8000 未运行） |
| Bot Runtime | 4 个 `pasay_bot.main` 实例（多 venv），需收敛为单 polling 实例；STATE_DB=`D:\AI-Review\pasay-pm\pasay-telegram-bot\state\bot_state.db` |
| 本机 Hermes gateway | gateway_state.json 显示 pid 25700，但进程已不存在（stale）；Hermes app/serve 运行中 |

### 分支分叉（Windows vs Mac）

- 共同祖先：`5e1b955`（BRIDGE-ROUTER-002）
- Mac 独有：`6688340`（SLICE3-UX-PERSISTENT-MENU-002）、`2bc4c7c`（WF-GUARDRAILS canonical 同步）— 9 文件，+444/-18
- Windows 独有：`cb7dfc0`（fix: preserve editable status messages）、`9d63a3e`（guardrails 确定性实现）— 22 文件，+2064/-147
- Windows WIP（未提交）：Bot V1 最新工作（expense flow / NL queries / UX cards / callback / api_client / 测试），**必须保留**
- 冲突策略：bot 代码冲突保留 Windows WIP（今天最新 Bot V1）；Mac 新增测试与规则保留合并

---

## 3. 迁移映射（Mac → Windows）

| Mac 路径 | Windows 目标 | 转换 |
|---|---|---|
| `~/.hermes/AGENT_PLAYBOOK.md` | `C:\Users\Admin\AppData\Local\hermes\AGENT_PLAYBOOK.md` | `~/.hermes` → `%HERMES_HOME%` |
| `~/.hermes/SOUL.md` | Windows Hermes `SOUL.md` | 合并：保留 Windows 执行纪律 + 增加 Pasay orchestrator 指针 |
| `~/.hermes/plans/*.md` | `%HERMES_HOME%\plans\*.md` | 路径转换 |
| `~/.hermes/memories/MEMORY.md, USER.md` | 合并进 Windows memories | 只追加 Pasay 上下文，保留 Windows 本机知识；路径/命令转 Windows |
| `~/.hermes/skills/productivity/property-management` | `%HERMES_HOME%\skills\productivity\property-management` | BASE `localhost:8000` → `127.0.0.1:8001`；`python3` → Windows python；API key 沿用 Windows secret |
| `~/.hermes/skills/productivity/pasay-pm-data-model` | `%HERMES_HOME%\skills\productivity\pasay-pm-data-model` | 同上 |
| `~/.hermes/skills/software-development/telegram-bot-engineering` | `%HERMES_HOME%\skills\software-development\telegram-bot-engineering` | 路径转换 |
| `~/.hermes/skills/autonomous-ai-agents/multi-agent-product-engineering` | `%HERMES_HOME%\skills\autonomous-ai-agents\multi-agent-product-engineering` | playbook 路径 → Windows |
| `~/.hermes/skills/autonomous-ai-agents/codex` | `%HERMES_HOME%\skills\autonomous-ai-agents\codex` | codex executable 路径 → Windows Codex CLI |
| `~/.hermes/profiles/pasay-property/plugins/property` | `%HERMES_HOME%\plugins\property`（按需） | 参考 |
| `~/.hermes/cron/jobs.json`（property profile） | Windows Hermes cron（按需，保持 paused 或与 Windows gateway 能力匹配后启用） | deliver/路径转换 |

不复制：Mac PATH、launchd、Unix socket、macOS 服务、cache、session 临时文件、venv、node_modules、build cache、无关大文件。

Secrets 处理：API key / Telegram token / SSH 凭据不打印、不回传。Windows 已有对应 secret（repo `.env`、bot `.env`、Hermes `.env`）则沿用 Windows secret。
