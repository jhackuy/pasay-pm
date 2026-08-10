# Phase 1 — 审计任务给 Codex Max (Principal Engineer)

你在代码库 `~/Documents/Codex/pasay-pm` (PASay Property Management) 工作。这是**只读审计阶段,不要修改任何代码**。除了产出文档,不要改动 git 树,不要跑会写数据库的东西。

## 背景（已确认的事实，用这些来验证而不是怀疑）

- 项目是 FastAPI + PostgreSQL16 后端。`git main` 当前 commit = `746b605`。
- 运行时后端部署在 `/opt/pasay-pm`(native uvicorn, brew postgres),开发 git 树在 `~/Documents/Codex/pasay-pm`。
- **当前仓库里没有任何原生 Telegram Bot 代码**。已 grep:没有 aiogram / python-telegram-bot / InlineKeyboard / callback_query / sendMessage / edit_message_text。
- 现在的「Telegram Bot」实际是 **Hermes Agent gateway**(LLM)通过 `property-management` skill 的 `property_client.py` 用 HTTP Bearer 调 Pasay API,然后在 Telegram 用文字 `sendMessage` 回显。ASCII 表格、Markdown 格式化、确认卡片(card)都是 **LLM 现生成的文字**,不是确定性 renderer。
- Hermes 网关在 `~/.hermes/`(主)和 `~/.hermes/profiles/pasay-property/`(受限群 bot),两个都是 **long polling** 生产 bot token(@zhushoumacbot 主 DM + @pasayhousebot 受限群)。
- property 业务逻辑在 skill:`~/.hermes/skills/productivity/property-management/`(SKILL.md + scripts/property_client.py + assets/config.env/roles.json)。
- 后端已有:income/confirm/reverse、audit log、RBAC(admin/manager/agent 通过 API key + telegram_user_id→role)、reports(overdue-rents/financial-summary)、commission 引擎。金额用 Decimal/numeric。
- roles: OWNER(5177241442,中文) 全权;SECRETARY(1083657401,英文) 录收入/交费/维修但**不能** confirm income。

## 架构决策（用户已批准，必须遵循）

新增**原生 Telegram Bot 层**:`pasay-telegram-bot`。
- **同仓库 + 独立 service**(除非你找到明确技术理由,否则不建第二套项目)。
- Native Bot 是生产 pasay bot token 的**唯一 consumer**;Hermes 停止 polling/webhook 该 token(scoped cutover,防 409 Conflict)。
- Native Bot 处理所有:InlineKeyboard、callback、pagination、renderer、conversation state、二次确认。
- 财务写操作最终调 **Pasay PM API**;Native Bot **禁止直接写 PostgreSQL**。
- Hermes 只做自然语言理解、复杂 reasoning、tool 编排;确定性页面不要让 LLM 自由生成。
- 目标架构:
```
Telegram
   ├─ pasay-telegram-bot ── deterministic commands/buttons ──→ Pasay PM API
   └─ natural language ──→ Hermes ── intent/tool routing ──→ Pasay PM API
          (processed structured result 回 Native Bot renderer 输出 UI)
```

## 本轮范围（只做 房源/财务/逾期/收租 四块）

房源、财务、逾期、收租。维修、佣金、租约编辑等只保留入口/现状,不重构。

## 你的审计任务（读代码给出结论，不要改代码）

### 1. 必答 17 问（针对当前架构 + 全链路）
```
Telegram Update → Telegram Handler → Hermes/LLM/Command Parser → Pasay PM API → PostgreSQL → Response → Formatter → Telegram sendMessage/editMessage
```
1. Telegram Bot 入口在哪里？现在实际入口是哪个进程/文件？
2. 当前用哪个 Python/Node Telegram library？（现在其实是 Hermes 网关 —— 你确认具体用什么发消息）
3. sendMessage / reply_text / edit_message 在代码/ Skill 哪里？
4. 当前 parse_mode：Markdown / MarkdownV2 / HTML / None？
5. 截图里 ASCII 表格谁生成的？LLM 还是业务 formatter？
6. 是否已有统一 renderer？
7. 是否已用 InlineKeyboardMarkup？callback_query？
8. Telegram callback state 如何保存？
9. 房源/财务/租金/逾期分别对应后端哪些 API/router？
10. 收租 confirm/reverse 当前链路是什么（看 app/api/routers/income.py + property_client.py record_rent_pending/confirm_rent/reverse_income）？
11. 当前 API Key / RBAC 如何实现（看 app/api/deps.py, app/core/security.py, auth.py, models/user.py）?
12. 当前审计日志如何实现（app/services/audit.py + routers/audit.py）?
13. 是否存在重复写入风险？（看 income 是否有唯一约束 / idempotency key / 防重复 confirm）
14. 是否存在直接 DB 写入绕过 API 的代码？
15. expense 审批流和 reverse 的权限边界是什么？
16. 现有测试覆盖（tests/,103个）与缺失项。
17. 列出准备实现 Native Bot 时会改/新增的文件清单。

### 2. 用户要求的 A–H 补充审计
- A. Pasay PM 当前真实部署位置（/opt/pasay-pm 的部署结构、LaunchDaemon、bin/start-native-api.sh 如何被系统拉起）
- B. Docker / repo 目录结构（docker-compose.yml 现状、Container 是否已停、alembic 迁移目录）
- C. 当前生产 pasay bot token 在哪个 Hermes gateway 注册（主 ~/.hermes/.env 的 TELEGRAM_BOT_TOKEN vs 受限 profile）
- D. polling 还是 webhook（确认两者都是 long polling）
- E. Hermes 是否已有可供 Native Bot 调用的 local HTTP/session API
- F. 如果没有,设计**最薄**的 Hermes adapter（Native Bot 如何把自然语言请求送进 Hermes、拿到 structured result、回传给 renderer）
- G. 当前 property skill/plugin 能否复用（SKILL.md/property_client.py 能否继续作为 Hermes 端 adapter）
- H. 当前 Telegram 用户/群 → Hermes session 是如何映射的（allowed_users/allowed_chats/telegram_user_id→role）

### 3. 输出设计文档（写到仓库，不提交 git）
产出两个文件（README 风格 markdown，放仓库根或 docs/）：
- `CURRENT_ARCHITECTURE.md`：当前架构、主要文件、调用链、当前问题、风险、推荐重构方案、准备改/新增的文件清单。
- `NATIVE_BOT_DESIGN.md`：`pasay-telegram-bot` 的最终目录结构、service 设计、依赖选择（python-telegram-bot v21+ 还是 aiogram，给出理由）、与 Hermes 的 adapter 接口、callback_data 编码方案、conversation state 存储、idempotency 方案、切流(scoped cutover 阶段)方案、测试策略。

### 4. 关于 Telegram 能力（必须实际核实，不要凭模型记忆）
不需要我替你联网，代码里就能确认当前 parse_mode。请确认：
- 当前 Bot 用 MarkdownV2 还是 HTML 还是 None。
- Telegram **原生不支持** Markdown 表格（客户端可视格式），这是事实；本轮不依赖 Markdown table。
- 确定目标用 **HTML parse_mode + 卡片式 + InlineKeyboard**。

### 5. 明确要求
- 除文档文件外**不要改任何代码/配置**。
- 修改 git 树前先 `git status` 确认,只新增文档,不 commit（等 Hermes orchestrator 统一 commit）。
- 完成后输出一份简短总结：审计结论 → 你推荐的技术栈 → 最终目录/服务设计 → 建议的切流步骤。

现在开始。先 `git status` 和 `git log -3` 确认基线，再审代码，再写两份文档。
