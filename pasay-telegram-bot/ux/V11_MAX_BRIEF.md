# PASay Telegram Bot — V1.1 UX-First Overhaul (Codex Max Brief)

你 (Codex Max) 是 Principal Engineer + UX Implementation Lead + Reviewer。
本任务是**工程执行型**：审计 → 实现 → 测试 → 自审 → 二轮 review，自主完成到可验收。

目标不是加功能，而是让现有 @pasayhousebot 变成「打开就会用、不记命令、高频 2–3 点击、每早一看就知道该做什么」。用户体验 > 技术炫技、可用 > 功能数、自动 > 手工输入、按钮 > 命令、确定性 > LLM。

---

## 0. 工作范围与环境事实（Hermes 已核对）

- **Git 工作根**：`/Users/jhackuy/Documents/Codex/pasay-pm`（这是唯一可运行的 git 目录，**你在此工作**）。bot 代码在 `pasay-telegram-bot/` 子目录。
- **生产运行时副本**：`/opt/pasay-pm/pasay-telegram-bot/`（由 Hermes 负责同步 + launchd 重启，你不用碰 /opt）。
- **生产后端 API**：`http://localhost:8000`，前缀 `/api/v1`，本地已运行（FastAPI）。首页 dashboard 需要的数据端点都已在：
  - `GET /api/v1/reports/financial-summary?month=YYYY-MM` → 本月应收/已收/未收
  - `GET /api/v1/reports/overdue-rents` → 逾期租金（含 overdue_days, total_outstanding, 按 lease）
  - `GET /api/v1/reports/tasks` → 任务/待办
  - `GET /api/v1/units`、`GET /api/v1/leases`、`GET /api/v1/properties`、`GET /api/v1/tenants`、`GET /api/v1/incomes`
- **测试基线（不得减少）**：backend 102 passed / bot 101 passed。Python 3.11。venv: `pasay-telegram-bot/.venv`。**运行任何 python/pytest 前请 `env -u PYTHONPATH`**（避免外 venv 污染，见 skill 陷阱；测试也需如此）。
- **当前 bot 结构**：
  - `pasay_bot/handlers/commands.py`（332 行）— /start→show_menu（纯 4 按钮，无数据）、properties/finance/overdue/rent/pending 的 page builder
  - `pasay_bot/handlers/callback.py`（714 行）— callback 路由
  - `pasay_bot/render/cards.py`（281 行）— 集中 renderer
  - `pasay_bot/render/i18n.py` — zh/en key 集
  - `pasay_bot/render/html.py` — html.escape + truncate + money
  - `pasay_bot/keyboards.py`（311 行）— 键盘构造
  - `pasay_bot/handlers/edit_utils.py` — 已有 `edit_message_text_idempotent`（吞 "Message is not modified"）
  - `pasay_bot/state/store.py` / `idempotency.py` — conversation state + idempotency keys（已有）
  - `.env` 在 bot 目录下，含 API key 与 bot token（勿打印）
- **RBAC/角色**：roles.py 有 OWNER/SECRETARY，中文/英文 locale 按用户。复核任何写操作都需走既有权限。
- **收租现状**：`show_rent` → property list → unit list → callback → 表单。金额/账期/日期/付款方式都要用户选。

---

## 1. 阶段 A — UX 审计（先做，别急着改）

先只读审计当前生产 bot 的交互面（读代码 + 可选 mock 渲染，不用真发消息）。输出 **`ux/UX_AUDIT.md`**（放 `pasay-telegram-bot/ux/`），按 **CRITICAL / HIGH / MEDIUM / LOW** 分类，针对这 20 条，每条给「现状 + 证据(文件/行) + 建议」：

1. /start 当前页面
2. 主菜单按钮
3. 房源页面
4. 财务页面
5. 逾期页面
6. 收租流程
7. 返回路径
8. 取消路径
9. 消息是否刷屏
10. 过多使用新消息而非 edit
11. 无意义确认
12. 必须输入文字的地方
13. 可自动默认的内容
14. 新用户首入是否知道下一步
15. 空数据页面显示
16. 错误能否恢复
17. 已完成操作是否仍显示可执行按钮
18. 已付款 Unit 是否仍显示「登记收租」
19. 空置房是否出现无意义操作
20. 每页按钮是否过多

## 2. 阶段 B — 实施本轮明确范围

按用户规范落实（摘要 + 你需补的工程细节）：

### B1 首页 = 「今日管理中心」(替代纯 4 按钮)
- `/start` 不再是说明书 + 4 按钮，而是 dashboard。首入欢迎语 1 行 + 今日首页。
- 数据全部来自 API，**禁止 LLM 推测**。结构（zh/en 双份）：
  - 🏠 Pasay 房产管理 / 📅 日期 / 💰 本月租金(应收·已收·未收) / ⚠️ 今日待处理(逾期笔数·即将到期租约·维修待办) / 🏘 空置数
  - 按钮：`[💵 收租] [⚠️ 待处理] [🏘 房源] [📊 财务]`
  - 无待办时显示「✅ 今天没有紧急事项」，**不显示一堆 0**（空/0 值隐藏）。
- 关键工程点：dashboard 聚合多个 API，**用 asyncio.gather 并行**减少等待；可容忍的 nice-to-have（如维修数）缺后端就隐去，不造假。

### B2 「待处理」(pending) 作为核心入口
- 聚合：逾期租金、即将到期租金/账期、即将到期租约、已有维修/审批待办（后端有就用，没有就不显示）。
- 原则：显示真实可取数据，不要为了完整造假。

### B3 提醒聚合不刷屏
- 把「连续发 N 条」改成「1 条汇总 + [查看全部]」。本轮做好 renderer/聚合数据结构，**不要建高频提醒 cron**（避免刷屏；是否开定时主动提醒留后续）。

### B4 收租流程压缩（重点）
- 目标：首页 → 💵收租 → 选未付款 Unit → 确认 ≈ 3 步。别走「物业→楼栋→Unit→月份→日期→金额→付款方式→确认」。
- **智能默认**：账期=当前月、日期=今天、金额=当前应收(自动带出)、付款方式=用户最近一次(可存用户级默认)。
- 选择列表：未付款优先、逾期 Unit 最前、已付款隐藏或后置、空置不显示收租。
- 确认页把默认值和「银行收款正在确认中」这类文案直接呈现，最终确认页给 `[✅确认][修改][取消]`。
- 默认值可减少点击，但**不能绕过最终财务确认**。

### B5 状态驱动按钮
- 未付→`[✅登记收租]`；已付→`[💰查看付款]`；已撤销→`[🔄重新登记]`；空置→**不显示**「登记收租」；过期账期→不允许旧按钮直接写入。已完成的旧消息不得仍显示可执行按钮。

### B6 减少刷屏（edit 优先）
- 导航类 UI → edit 原消息；财务写成功 → 可独立一条确认/审计友好消息。沿用 `edit_message_text_idempotent`。
- 明确一个规则写进 renderer/文档：哪些操作 edit、哪些 send。

### B7 每个页面有返回/取消
- 二级页面至少 `[⬅️返回]`；写操作中途 `[❌取消]`。session 过期给 `⚠️此操作已过期 [🏠返回首页]`（不强制重新 /start）。

### B8 错误可恢复
- 错误页给下一步：`[🔄重试][🏠首页]`。财务写不确定：`⚠️正在确认交易结果，请勿重复提交` → 自动查最终状态 → `✅已确认收租`。沿用 idempotency 防止重复写入。

### B9 空状态 UX
- 空逾期/空置/无待办 → 正面短句 + `[🏠返回首页]`。禁止 No data / [] / 0 records。

### B10 语言与文案
- 中/英 i18n 保持。文案短、自然、非技术化。如「Transaction confirmed successfully.」→「✅ 收租已登记」；「Callback expired.」→「⚠️ 这个操作已经过期，请重新打开当前页面。」

### B11 响应速度
- 检查 callback 时长、API 调用次数、重复查询、串行 API → 并行、无意义 LLM 调用。**页面按钮禁止经过 LLM** 本就不该有，确认无渗入。
- 数据加载有等待 → 先 `answerCallbackQuery`（必要时 `⏳正在加载…`）再 edit。不让 Telegram 一直转圈。

### B12 不做复杂 NL（本轮）
- 不引入 Hermes 自然语言 adapter。保持架构未来可插 NL→intent→API→同一 renderer，**不要建第二套 UI**。确认 nl_bridge 不破坏确定性菜单。

---

## 3. 阶段 C — 测试

保持 backend 102 / bot 101 不减。**新增 UX 测试**，至少覆盖这 22 条（命名可贴合既有测试风格）：
`test_start_shows_dashboard`, `test_dashboard_no_tasks`, `test_dashboard_with_overdue`, `test_dashboard_zero_values_hidden`, `test_paid_unit_has_no_collect_button`, `test_vacant_unit_has_no_collect_button`, `test_overdue_unit_priority`, `test_back_button_every_page`, `test_cancel_write_flow`, `test_expired_state_home_button`, `test_empty_overdue_state`, `test_empty_property_state`, `test_edit_navigation_no_message_spam`, `test_collect_default_current_period`, `test_collect_default_amount`, `test_collect_default_today`, `test_double_click_still_idempotent`, `test_callback_ack_fast`, `test_api_error_retry_button`, `test_uncertain_payment_state`, `test_i18n_zh`, `test_i18n_en`。
沿用现有 no-network harness（fake bot + fake backend）。新增后跑 `env -u PYTHONPATH .venv/bin/python -m pytest tests/ -q` 全绿。

## 4. 阶段 D — Git diff review + 二轮 review

- **自己**做一轮 git diff review，修复自会发现的问题。
- 然后**换身份**：Senior Product Engineer + UX Reviewer，针对这 14 个问题逐条过完整流程，发现问题直接修复：
  1 首次用户要学习吗 2 要记命令吗 3 首页 5 秒看懂吗 4 高频任务超过 3 击吗 5 有可自动却让用户选的吗 6 按钮太多吗 7 有技术术语吗 8 有死胡同吗 9 有错误无法恢复吗 10 有刷屏吗 11 有重复信息吗 12 已完成状态还显示错误按钮吗 13 用户重复输入已知信息吗 14 有「工程师觉得合理普通人觉得复杂」处。
- 同时做**安全/健壮性复核**：双击幂等、timeout-before/after 写、重复 create、stale callback、手改 callback_data 越权、空数据、长文案、金额 0/0.01/大额/负数、HTML 转义。修复 CRITICAL/HIGH。
- 把二轮 findings 追加到 `UX_AUDIT.md` 或单独 `ux/UX_REVIEW2.md`。

---

## 5. 交付物清单（完成后确认都有）

1. `pasay-telegram-bot/ux/UX_AUDIT.md`（阶段 A 全量分类）
2. 实现的代码改动（首页 dashboard、待处理聚合、收租压缩+默认值、状态按钮、edit 优先、返回/取消、错误恢复、空状态、i18n）
3. 新增 UX 测试（≥22 条名目覆盖）
4. `pasay-telegram-bot/ux/UX_REVIEW2.md`（二轮 UX + security 复核 findings）
5. 最终纯文本总结（terminal stdout 输出），包含：
   - UX 审计关键发现（按 CRITICAL/HIGH 摘要）
   - 改了哪些用户流程
   - 首页 before/after 文案对比
   - 收租 before/after 步骤对比 + 原多少击 → 现在多少击
   - 哪些输入被自动化、哪些页改为 edit、哪些错误加了恢复入口
   - 新增几项 UX tests + bot 总测试数 + backend 总测试数（以你实测为准）
   - 二轮 review findings
   - 当前剩余 UX 问题
   - 下一阶段最值得做的 3 件事

## 6. 纪律

- **不要伪造数据/测试结果**。测试必须真实跑绿。
- 代码风格对齐既有（singleton renderer、i18n、Decimal、html.escape、idempotency）。
- 不要改动 backend（backend 现有端点够用）。
- 工作只在 git 源完成；**不要 commit 到 main**，工作在当前分支即可；如新建分支也保持独立，由 Hermes 统一同步 /opt 与重启。
- 金币口径：金额一律按原始币种（PHP）原样，`₱` 前缀 + 千分位，无 0 尾巴（0.01/0/大额/负数都要处理正确）。
- 若中途发现某能力后端确实没有，注明「后端不支持，本轮隐藏」而非硬造。
