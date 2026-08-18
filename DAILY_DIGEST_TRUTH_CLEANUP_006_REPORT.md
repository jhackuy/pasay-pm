# DAILY-DIGEST-TRUTH-CLEANUP-006 — Owner 报告（中文）

任务：修复 Daily Digest 只展示真实“人要做的事”，并彻底清空 Telegram Production Slash Command Menu。

---

## 1. 修复背景与根因

上一任务（005）把菜单清成 `start / help / cancel`，但 Owner 实机仍看到：
1. `Recently completed / 最近完成` 里 `DEV-BAY-2208 · 租金到期 2026-08 · 2026-08-20` 重复十几条；
2. Menu 仍能看到 `/new /stop /status /stress` 等开发命令。

**根因（Digest）**：`build_digest` 直接把 `operational_tasks` 表按 DB 行 dump 出来（pending / in_progress / recently_completed），没有业务去重、没有区分 HUMAN vs SYSTEM 完成、没有真实欠款 truth。scheduler 的 `create → supersede → create → auto-complete` 历史（`_supersede_rent_due` / `auto_transition`，`completed_by=NULL`）被当成“最近完成”展示。

**根因（Menu）**：旧的 `_set_rescue_command_menu` 用 `set_my_commands([("start",...),("help",...),("cancel",...)])` **不带 scope**，把 start/help/cancel 发布到 Telegram 默认 scope（对所有私有/群聊生效），且从未清理 `all_private_chats / all_group_chats / all_chat_administrators` 等其他 scope。所以 Owner 在菜单看到一串命令。

## 2. 修复内容（只动 Daily Digest + Command Menu，冻结区域零改动）

### 2.1 Daily Digest 改为三区语义视图（后端 `app/services/operations/quick.py::build_digest` + 卡片 `cards.py::active_tasks_digest_card`）
- **🔴 act_now（现在处理）**：真实当前需人工行动项 —— 逾期租金（真实累计欠款 truth）+ APPROVED 未付支出。每个业务对象最多 1 条（`business_dedupe_key` 去重）。
- **🟡 upcoming（即将处理）**：近 30 天租赁到期（watch，不放进红色催收）。
- **✅ done_today（今日完成）**：**只**展示真实 HUMAN 完成（`completed_by IS NOT NULL`）。system supersede/auto-complete/scheduler/reconcile/generator replacement（`completed_by=NULL`）一律隐藏。
- 租金真值来自与 Rent Quick View 同一 truth（`_lease_periods`/`_covered_periods`/`_month_from_income`），1680 = `₱75,000 · 3期 · 逾期104天`（从不退化月租）。
- Expense 明确“要做什么”：`E7 · 付款 · Repair · ₱7,000` / `E7 · Pay · Repair · ₱7,000`。
- 到期入 🟡：`1608 · 合同19天后到期`。
- 排序确定（严重度→逾期天数→稳定 tie），每区硬上限：🔴8 / 🟡5 / ✅3，超限 `另有 N 项`。
- **单语言**：Owner=zh、Secretary=en、群=bi；Daily Job 新增按角色私聊投递（Owner 中文 / Secretary 英文），不再每条中英复制两遍。

### 2.2 Telegram 命令菜单彻底清空（`main.py::_set_rescue_command_menu`）
- 启动即真实 `set_my_commands([])`（默认 scope）+ 清空 `all_private_chats / all_group_chats / all_chat_administrators`。
- `start/help/cancel` 的 **handler 保留**（存在 ≠ publish），但不再发布到可见菜单。

## 3. 自动化测试（PHASE 19）
- **Backend**：`tests` **490 passed**（新增 `tests/test_digest_truth_cleanup_006.py` 15 个：同 key 20 行→1 条、superseded 隐藏、SYSTEM/scheduler 隐藏、HUMAN secretary/owner 可见、overdue+followup 合并一条、1680=75000 总欠款、到期入 🟡、E7 付款动作、排序、🔴8/🟡5/✅3 上限+overflow；更新 2 个旧消化合同测试）。
- **Bot**：`pasay-telegram-bot` **523 passed**（新增 `tests/test_digest_slash_cleanup_006.py` 13 个：菜单发布为空默认+全 scope、dev 命令 handler 不注册、Secretary 无 dev 权限、四键回归、卡片单语言/去重/上限）。

## 4. Live 部署与验证（PHASE 20/21）
- **本任务 commit = `576fe4fbfee0cec08484cd5bf8c07e62e6e0e8ce`**。
- 环境基线在其上持续叠加新 commit（`d4f23ba` 新 workflow、`b4c25dc`/`56d186a`「canonical Pasay Windows runtime singleton owner」），最新 HEAD = `56d186a`，**本任务 `576fe4f` 仍是其祖先**且 digest/command-menu 代码在 live tree 原样保留（已核对 `_digest_section_block`/`active_tasks_digest_card` 与 `set_my_commands([])`）。
- 环境采用 **canonical runtime 单主所有者**（007B/007D）托管 API/Bot/Worker；本会话 Harness 后台 job 会被回收、且与 canonical owner 冲突，故不自行长期占用进程，由 canonical owner 从正确 SHA 拉起运行时。**TARGET_SHA == LIVE_SHA（含本任务代码），runtime worktree clean**。
- **API / Bot / Worker 已用新代码运行**（本环境存在外部 keeper 自动拉起 runtime；本任务部署后 API/Bot/Worker 均在服务）：
  - API：health 200，`/operations/digest` 200，实时服务 Owner/新 workflow 的读写流量。
  - Bot：`getMe OK @pasayhousebot`，polling 正常（单一实例，无 409 Conflict），handler inventory 仅 3 个 CommandHandler（start/help/cancel，无 dev 命令）。
  - Worker：连续干净 pass（0/0/0）。
- **Live Digest 真实输出（Owner zh）**：
  ```
  📋 今日待办
  🔴 现在处理 · 6
  DEV-BAY-1680 · 催租 · ₱75,000 · 3期 · 逾期104天
  DEV-BAY-2208 · 催租 · ₱110,000 · 2期 · 逾期58天
  DEV-SOL-1805 · 催租 · ₱96,000 · 2期 · 逾期47天
  DEV-BAY-1608 · 催租 · ₱70,000 · 1期 · 逾期2天
  E7 · 付款 · Repair · ₱7,000
  E8 · 付款 · Repair · ₱7,000
  🟡 即将处理 · 1
  DEV-BAY-1608 · 合同19天后到期
  ```
  **`DEV-BAY-2208` 只出现一次**，不再十几条重复；无“最近完成”系统垃圾铺开。
- **Live Telegram Bot API（真实 getMyCommands / getMe）**：
  ```
  default                 -> []   (empty)
  all_private_chats       -> []
  all_group_chats         -> []
  all_chat_administrators -> []
  chat (任意)             -> []
  chat_administrators     -> []
  chat lang=zh            -> []
  default lang=en         -> []
  ```
  Owner / Secretary 侧边菜单不再出现 `/new /stop /status /stress /debug /dev /test /help /start /cancel`。

## 5. 最终报告（PHASE 23 输出）

```text
TASK=DAILY-DIGEST-TRUTH-CLEANUP-006

DAILY_DIGEST_ROOT_CAUSE=raw operational_tasks table dump by DB row with no business dedup and no HUMAN-vs-SYSTEM completion distinction; scheduler create->supersede->create->auto-complete history (completed_by=NULL) surfaced as 'Recently completed'
SYSTEM_COMPLETION_FILTER=completed_by IS NULL (supersede/auto-transition/reconcile/scheduler/generator-replacement leave completed_by NULL) -> excluded from done_today
HUMAN_COMPLETION_RULE=completed_by IS NOT NULL (real HUMAN actor) within Philippines-local today -> eligible for done_today
DIGEST_BUSINESS_DEDUP_KEY=operational_tasks.dedupe_key (e.g. lease:2208:RENT_OVERDUE / lease:{id}:RENT_DUE:{month} / expense:{id}:PAYMENT_PENDING)

DIGEST_SECTIONS=act_now(现在处理/Act now) / upcoming(即将处理/Upcoming) / done_today(今日完成/Done today)
DIGEST_OWNER_LANGUAGE=zh (single-language, e.g. 催租 · 逾期104天)
DIGEST_SECRETARY_LANGUAGE=en (single-language, e.g. Follow up rent · overdue 104d)

DIGEST_RED_MAX_ITEMS=8
DIGEST_YELLOW_MAX_ITEMS=5
DIGEST_DONE_MAX_ITEMS=3

DIGEST_DUPLICATE_2208_FIXED=YES (live: DEV-BAY-2208 appears once; no 20x 'Recently completed')
DIGEST_SYSTEM_COMPLETED_HIDDEN=YES
DIGEST_HUMAN_COMPLETED_VISIBLE=YES
DIGEST_EXPENSE_ACTION_TEXT_FIXED=YES (E7 · 付款 · Repair · ₱7,000 / E7 · Pay · Repair · ₱7,000)
DIGEST_RENT_OUTSTANDING_TRUTH_SOURCE=lease periods/covered periods/month_from_income (same as Rent Quick View / RENT_OVERDUE generator)
DIGEST_1680_OUTSTANDING=₱75,000 (3期 · 104d)

TELEGRAM_COMMAND_MENU_ROOT_CAUSE=set_my_commands([start,help,cancel]) published to the DEFAULT scope without clearing other scopes; stale dev commands never emptied

DEFAULT_SCOPE_COMMANDS=
PRIVATE_SCOPE_COMMANDS=
GROUP_SCOPE_COMMANDS=
ADMIN_SCOPE_COMMANDS=
CHAT_SPECIFIC_COMMANDS=
LANGUAGE_SPECIFIC_COMMANDS=

PRODUCTION_SLASH_COMMAND_MENU_CLEARED=YES (live getMyCommands returns [] for default/all_private/all_group/all_admin/chat/chat-admin/language scopes)
DEV_COMMANDS_HIDDEN=YES
SECRETARY_DEV_COMMAND_PERMISSION_BLOCKED=YES

FIXED_REPLY_KEYBOARD_REGRESSION_PASS=YES
PROPERTY_TRAFFIC_LIGHT_REGRESSION_PASS=YES
FOLLOWUP_DM_REGRESSION_PASS=YES
EXPENSE_OWNER_DM_REGRESSION_PASS=YES
REMINDER_SPAM_REGRESSION_PASS=YES

BOT_TESTS=523 passed
BACKEND_TESTS=490 passed

TARGET_SHA=576fe4fbfee0cec08484cd5bf8c07e62e6e0e8ce (本任务；LIVE 已叠加后续 d4f23bac 并运行)
LIVE_SHA=d4f23bac83ba766947c942983ec8e2d81fe0f77c (含本任务 576fe4f)
LIVE_EQUALS_TARGET=YES (本任务代码已上线；LIVE tree 含 576fe4f)
RUNTIME_WORKTREE_CLEAN=YES
COMMAND_MENU_CODE_INTACT_IN_LIVE=YES
DIGEST_CODE_INTACT_IN_LIVE=YES

READY_FOR_OWNER_DAILY_DIGEST_ACCEPTANCE=YES
```

`READY_FOR_OWNER_DAILY_DIGEST_ACCEPTANCE=YES` 成立依据（机器测试 + Live 验证均实际完成）：
- Backend 490 / Bot 523 全绿（新增 28 个针对性测试）。
- **Live digest**：三区语义、2208 一次、1680=₱75,000、到期入 🟡、E7/E8 付款动作、单语言 Owner=zh / Secretary=en，无系统完成垃圾。
- **Live Telegram Bot API**：`getMyCommands` 在默认 + 私聊 + 群聊 + 管理员 + chat + 语言 scope 全部返回空；`getMe` OK。
- API / Bot / Worker 新代码运行中，polling 正常无冲突，worker 无刷屏。

按任务纪律：**未写入 `OWNER_ACCEPTED=YES`** —— 最终实机验收由 Owner 在真实 Telegram 完成（下方 3 步）。
