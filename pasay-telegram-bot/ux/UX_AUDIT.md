# PASay Telegram Bot — UX Audit (V1.1, Phase A)

Auditor: Codex Max (Principal Engineer + UX Implementation Lead)
Date: 2026-08-10
Scope: current production interaction surface of `pasay-telegram-bot` on branch `feature/telegram-ui-v2` (commit `cd750f3`). Read-only audit of code + rendered output via the existing FakeBot harness (no real messages sent).

Severity scale: CRITICAL = blocks the core daily task or misleads about money;
HIGH = wastes significant user effort / dead end / stale action;
MEDIUM = friction; LOW = polish.

---

## CRITICAL

### C1. `/start` 是纯按钮说明书，无任何数据 —— 每早一看不知道该做什么
- 现状: `/start` → `cmd_start` → `show_menu`，只渲染「主菜单 + 请选择功能：」和 4 个按钮。没有本月租金、逾期、空置、待办信息。
- 证据: `pasay_bot/handlers/commands.py:35-40` (`cmd_start`), `commands.py:122-128` (`show_menu`), `pasay_bot/keyboards.py:126-150` (`menu_keyboard`)。
- 建议: `/start` 改为「今日管理中心」dashboard：🏠 标题 + 📅 日期 + 💰 本月租金(应收/已收/未收) + ⚠️ 今日待处理 + 🏘 空置数；无待办显示「✅ 今天没有紧急事项」。按钮改为 `[💵 收租] [⚠️ 待处理] [🏘 房源] [📊 财务]`（B1）。

### C2. 收租流程 7+ 击且 2 处必须打字 —— 高频任务远超 2-3 击目标
- 现状: `💵收租` → 选物业 → 选 Unit → Unit 页 → `💵记一笔` → **输入金额** → **输入日期** → 选付款方式 → 确认。9 步交互，其中金额/日期为自由文本输入。
- 证据: `commands.py:208-221` (`show_rent`), `commands.py:307-332` (`show_rent_units`), `pasay_bot/keyboards.py:152-218`, `pasay_bot/handlers/conversation.py:39-105` (`_enter_amount`/`_enter_date`)。
- 建议: 首页 → `💵收租` → 未付款 Unit 列表(逾期最前) → 确认页 ≈ 3 击；金额=当前应收、日期=今天、账期=当月、方式=最近一次，全部自动带出，最终仍需 `[✅确认]`（B4）。

### C3. 回调导航全部发送新消息，菜单反复点击刷屏，旧卡片残留可执行按钮
- 现状: `nav`/`pg` 回调走 `pages.show_*` → `_send` → `bot.send_message`，每次点击产生一条新消息；旧消息的按钮永远可点。
- 证据: `callback.py:146-180` (`_handle_nav`/`_handle_page`), `commands.py:130-134` (`_send`)。对比 `_handle_rent` 已用 `show_rent_units`/`show_unit_page` 的 `edit_message_text_idempotent`。
- 建议: 导航一律 edit 当前消息；仅在命令发起或财务写成功时 send（B6）。规则写进 renderer/文档。

### C4. 状态无关按钮：已付款 Unit 仍显示「登记收租」
- 现状: `unit_page_keyboard` 只要 `can_rent=True` 就显示「💵 记一笔」，不看该 Unit 是否已付当月租金。
- 证据: `keyboards.py:186-203` (`unit_page_keyboard`), `commands.py:270-305` (`build_unit_page` 从不查询 incomes)。
- 建议: 状态驱动按钮——未付→`[✅登记收租]`；已付→`[💰查看付款]`；已撤销→`[🔄重新登记]`；无活跃租约/空置→不显示收租按钮（B5）。

### C5. 过期/错误回调只弹 toast，卡片仍像「可执行」，无恢复入口
- 现状: `_expired` 或权限失败时只 `_answer` 一条 toast，卡片按钮原样保留；超时错误也只 toast，用户不知下一步。
- 证据: `callback.py:96-100` (`_expired`), `callback.py:302-311` (expired → `_answer` only), `callback.py:647-653` (error → toast only)。
- 建议: 过期/错误时 edit 卡片为状态页 + `[🏠返回首页]`；API 错误给 `[🔄重试][🏠首页]`（B7/B8）。

---

## HIGH

### H1. 收租选择页没有「返回」，只有「取消」= 整个流程作废
- 现状: 物业列表、Unit 列表只有 `[❌取消]`；点错一步只能整体放弃重来。Unit 页「◀️ 返回」跳 `nav:rent`（新消息 + 回到第一步）。
- 证据: `keyboards.py:152-166` (`property_list_keyboard`), `keyboards.py:168-184` (`unit_list_keyboard`), `keyboards.py:196-203` (back → `nav:rent`)。
- 建议: 每个二级页至少 `[⬅️返回]`，写操作中途 `[❌取消]`；取消后给 `[🏠首页]`（B7）。

### H2. 空数据页面无下一步按钮
- 现状: 逾期空 =「🎉 暂无逾期租金」（无按钮）；房源空 =「暂无房源数据」（无按钮）；待确认空 =「🎉 暂无待确认收入」（无按钮）。
- 证据: `pasay_bot/render/cards.py:150-165` (`overdue_list`), `cards.py:70-78` (`properties_overview`), `cards.py:257-281` (`pending_list_card`)。
- 建议: 空状态 = 正面短句 + `[🏠返回首页]`，禁止 No data / [] / 0 records（B9）。

### H3. 主要页面无返回/首页按钮（房源/财务/逾期/收租）
- 现状: 这些页面只有内容 + 分页或操作按钮，无法直接回首页。
- 证据: `commands.py:158-172` (`show_properties`), `commands.py:174-185` (`show_finance`), `commands.py:188-206` (`show_overdue`), `keyboards.py:236-258` (pagination only)。
- 建议: 所有二级页底部加 `[🏠 首页]`（B7）。

### H4. 必须文字输入的地方：金额 + 日期
- 现状: 收租必须输入金额和日期文本（`conversation.py:39-105`），且输入错误只回一条提示文本，无按钮重试。
- 证据: `conversation.py:65-77` (`_enter_amount` 错误提示), `conversation.py:89-99` (`_enter_date` 错误提示)。
- 建议: 默认金额/日期直接带出；「修改」子流程才需要输入，错误提示配 `[🏠首页]`（B4/B7）。

### H5. 可自动默认的内容没有默认
- 现状: 账期、日期、金额、付款方式全部手动。`_begin_rent_entry` 已取到 `monthly_rent` 却仍让用户输入。
- 证据: `callback.py:210-243` (`_begin_rent_entry` payload 含 `monthly_rent`), `conversation.py:39-105`。
- 建议: 账期=当月、日期=今天、金额=lease.monthly_rent、方式=用户上次使用（存 user_defaults）；默认值可减击但不可绕过最终确认（B4）。

### H6. `/pending` 只聚合「待确认收入」，不是「待处理」
- 现状: `/pending` = OWNER 待确认收入列表，无逾期、无即将到期租约、无任务。
- 证据: `commands.py:223-267` (`show_pending`)。
- 建议: 聚合逾期租金、即将到期租约、维修/审批待办（后端 `/reports/tasks` 已有）、待确认收入；1 条汇总 + `[查看全部]`（B2/B3）。

### H7. 空置房显示「记一笔」→ 死胡同错误
- 现状: 空置 Unit 的 Unit 页仍显示「💵 记一笔」，点击后报「该 Unit 没有活跃租约」。
- 证据: `keyboards.py:186-203`, `callback.py:224-231` (lease None → error)。
- 建议: 空置/无活跃租约不显示收租按钮（B5）。

### H8. 错误文案与技术术语混杂、部分误导
- 现状: `common.invalid` =「已过期」被无效回调复用（无效 ≠ 过期）；`common.expired` =「请重新打开当前账期」；帮助文本以命令为主。
- 证据: `pasay_bot/render/i18n.py` (`common.invalid`/`common.expired`/`help.text`), `callback.py:71-80` (decode None → `common.invalid`)。
- 建议: 文案短、自然：「⚠️ 这个操作已经过期，请重新打开当前页面。」；无效操作独立文案；帮助弱化命令、强调按钮（B10）。

---

## MEDIUM

### M1. 响应速度：回调先加载后 ack，导航期间转圈
- 现状: `nav`/`pg`/`rn:prop`/`rn:unit` 先做 API（串行多次）再 `_answer`；部分页面多次串行请求（如 `build_unit_page` 串行 get_unit → get_properties → get_leases → get_tenants）。
- 证据: `callback.py:146-180`, `commands.py:270-305`, `commands.py:93-100` (build_finance_page 串行)。
- 建议: 导航类先 `answerCallbackQuery`；数据加载用 `asyncio.gather` 并行（B11）。

### M2. 主菜单缺「待处理」入口，逾期藏在第二按钮位
- 现状: 4 按钮为 房源/财务/逾期/收租；高频「待处理」没有独立入口。
- 证据: `keyboards.py:126-150`。
- 建议: 按钮改为 `[💵 收租] [⚠️ 待处理] [🏘 房源] [📊 财务]`（B1）。

### M3. 取消路径无下一步
- 现状: 取消后仅显示「❌ 已取消」，无首页按钮；`/cancel` 无会话时直接回主菜单（新消息）。
- 证据: `callback.py:530-541`, `commands.py:109-120`。
- 建议: 取消/完成后给 `[🏠首页]`（B7）。

### M4. 每页按钮数量与信息密度
- 现状: 逾期页每行 2 按钮 × 5 行 + 分页 ≈ 12 按钮，信息密集但可扫读；可接受。
- 证据: `keyboards.py:260-292`。
- 建议: 本轮保持每行 1 个主操作（收集列表单按钮），详情移入 Unit 页（B4）。

### M5. 重复查询与无用请求
- 现状: `show_pending` 每次 5 个 API 全量拉取再本地聚合；`build_overdue_page` 除主查询外再拉 units+properties。
- 证据: `commands.py:223-267`, `commands.py:190-201`。
- 建议: `asyncio.gather` 并行 + 复用 dashboard 已取数据（本轮并行化；跨页缓存留后续）。

---

## LOW

### L1. 帮助文本以命令为中心
- 现状: `help.text` 列出 `/start /properties /finance ...`，与「按钮优先」目标相反。
- 证据: `pasay_bot/render/i18n.py` `help.text`。
- 建议: 改为「点下方按钮即可；/help 查看更多」。（本轮保留命令兼容，文案弱化。）

### L2. `nl_bridge` 未知文本回「请用下方按钮」+ 菜单键盘（新消息）
- 现状: 未识别文本 → `common.unknown` + `menu_keyboard` 新消息。
- 证据: `pasay_bot/handlers/nl_bridge.py:64-83`。
- 建议: 保持确定性路由；菜单文案同步 dashboard（B12 确认无 LLM 渗入——已确认无）。

### L3. 分页页脚无返回首页入口
- 现状: `pagination.footer` 只有页码信息。
- 证据: `pasay_bot/render/html.py:158-166`。
- 建议: 页脚保持信息性，返回入口统一放键盘底部（B7 覆盖）。

---

## 阶段 A 结论

- CRITICAL 5 项全部落在：首页无数据(C1)、收租流程过长且必须打字(C2)、导航刷屏(C3)、状态无关按钮(C4)、过期/错误无恢复(C5)。
- 修复方向与 V1.1 brief B1-B12 一一对应；无「必须改后端」项——现有端点（`/reports/financial-summary`、`/reports/overdue-rents`、`/reports/tasks`、`/units`、`/leases`、`/incomes`）足够支撑 dashboard 与待处理聚合。
