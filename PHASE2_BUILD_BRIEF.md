# Phase 2 — 构建 `pasay-telegram-bot`（给 Codex Max, Principal Engineer）

你在 `NATIVE_BOT_DESIGN.md`（已由你产出）的架构基础上**实际实现**这套原生 Telegram Bot。
设计文档是你的蓝本，但**本 brief 中的硬性需求（尤其收租幂等、超时、旧按钮、权限）必须全部落实**。

工作目录：`~/Documents/Codex/pasay-pm`。新增独立 service 目录：`pasay-telegram-bot/`（同仓库，独立 venv，不新建 git 项目）。

## 铁律

1. Native Bot **禁止直接写 PostgreSQL**。所有财务写操作最终调 Pasay API（`POST /api/v1/incomes {status:pending}` → `POST /api/v1/incomes/{id}/confirm` → `reverse`）。
2. **禁止绕过 API 的收租写入**。Telegram UI 只是 API 的薄前置；callback 绝不能直接 UPDATE DB。
3. **金额一律 Decimal/字符串**（后端 `Numeric(14,2)`），禁止 float。
4. 确定性 UI：**HTML parse_mode + 卡片式 + InlineKeyboard**。所有用户/DB 文本 `html.escape()`。
5. 不许让 LLM 决定金额/状态/欠租/出租率——这些都来自 API。LLM（Hermes）只做 NLU 与解释。
6. 本轮范围：**房源 / 财务 / 逾期 / 收租** 四块。维修/佣金/租约编辑只保留入口/现状。

## 技术栈（按设计 §2/§12）
- `python-telegram-bot>=21,<23`（锁定 22.x）`run_polling()`。
- `httpx`（Pasay API + Hermes adapter）`;pydantic-settings`（config）;标准库 `sqlite3`（state）。
- 独立 venv：`pasay-telegram-bot/.venv`。

## 目录结构（按设计 §3，落实）
```
pasay-telegram-bot/
  pyproject.toml
  pasay_bot/
    __init__.py  main.py  config.py  api_client.py  roles.py
    keyboards.py          # InlineKeyboard 构造 + callback_data encode/decode（单一真源）
    render/html.py        # escape + 金额/日期/None/空数据/分页/4096 截断
    render/cards.py       # 房源/财务/逾期/收租 卡片
    handlers/commands.py  # /start /menu /help /properties /finance /overdue /rent
    handlers/callback.py  # callback 路由 + 状态机
    handlers/conversation.py
    handlers/nl_bridge.py # （本轮可实现为 stub + 调用 Pasay API 直答；Hermes adapter 后续接入）
    state/store.py        # SQLite conversations + idempotency_keys
    state/idempotency.py  # 写操作幂等
    bin/start-native-bot.sh
  tests/  .env.example  README.md
```

## 本轮 MVP：不加 Hermes adapter，但必须具备完整确定性 UI + 收租全链路

**重要取舍**：为降低复杂度和风险，本轮 Native Bot 的「自然语言输入」**不接 Hermes**，而是：
- 在 Bot 内做**确定性关键词/命令解析**（/properties /finance /overdue /rent + 采集中文/英文短语），命中 → 确定性页面。
- 未命中 → 回「请用下方按钮，或输入 /help 查看可识别指令」。
- Hermes NLU adapter（设计 §5.1 api_server）**留到后续阶段**，本轮不做。这符合「本轮只打通房源/财务/逾期/收租」。
- 如果你评估认为接 Hermes 不复杂且能在本轮完成，可以加，但**不要把自然语言当本轮成败关键**；按钮化收租才是硬目标。

## 必须实现的 UI 与流程（逐条对照任务 §6-§11）

### 房源 `/properties`（§6）
卡片式，每项目一块，不要空格对齐：
```
🏘 <b>房源概况</b>

🏢 <b>Pasay Premier Residences</b>
📍 5 Roxas Blvd
🚪 单元：1
🟢 已出租：1
⚪ 空置：0
...
━━━━━━━━━━
📊 总计：N 套
🟢 已出租：X
⚪ 空置：Y
出租率：85.7%
```
- 出租率 = occupied/units，Decimal 算，1 位小数。项目多时分页（每页 5–8 条）`[◀️ 上一页][▶️ 下一页]`。

### 财务 `/finance`（§7）
```
💰 <b>2026年8月财务</b>
🏠 租金
应收：₱363,000
已收：₱190,000
未收：<b>₱173,000</b>
收租率：52.3%
📈 收支
总收入：₱721,000
总支出：₱19,650
净收入：<b>₱701,350</b>
```
- 金额千分位；只有关键数字加粗；若逾期>0 加 `⚠️ 逾期租金：₱xxx`。数据来自 `GET /api/v1/reports/financial-summary?month=`（注意：该端点按当前账期返回，未收=0 只代表本月已清，见资产口径提示，不要把它当历史欠租全貌——历史欠租用 overdue-rents）。

### 逾期 `/overdue`（§8，行动导向）
```
⚠️ <b>逾期租金 · 3笔</b>
🔴 <b>Bayshore · Unit 16B</b>
租客：Juan Dela Cruz
应付：₱55,000
到期：2026-08-05
逾期：5天
[✅ 登记收租][📄 查看详情]
```
- 排序：逾期天数(desc) → 金额(desc)。
- 状态规范：🟢正常 🟡即将到期 🟠到期未付 🔴严重逾期 ⚪空置 🔵待处理。
- 不要把租客电话等敏感数据塞 callback_data。

### 主菜单 `/start` `/menu`（§9）
```
[🏘 房源][💰 财务]
[⚠️ 逾期][💵 收租]
```

### 收租 `/rent`（§10，本轮最重点）
点击 `💵 收租`：
1. 选物业 → 2. 选 Unit → 3. Unit 页面 → 4. 登记收租 → 5. 金额(默认当前应收，可改) → 6. 付款日期(默认今天) → 7. 付款方式(Bank/GCash/Cash/Other) → 8. **二次确认** → 9. 调 API。

收租数据来源：
- 物业列表：`GET /api/v1/properties`
- Unit 列表：`GET /api/v1/units`（按 property_id 过滤）
- Unit 活跃租约：`GET /api/v1/leases?unit_id=`（找 active）
- 月租/应收：从活跃 lease + 对应 rent income 得到；**当前应收**可用 `GET /api/v1/units/{id}` 或 overdue-rents 里的「应付」作为默认。
- 目标账期：默认当月；`due_day` 来自租约。
- 写路径：
  1. `POST /api/v1/incomes {status:"pending", lease_id, amount, received_date, payment_method, description:"rent <YYYY-MM>"}` → 返回 pending income（含 id）
  2. `POST /api/v1/incomes/{id}/confirm`
  3. audit 由后端 `confirm` 动作自动写（`record_audit action="confirm"`）。
- **绝对禁止**：用 `{status:"confirmed"}` 直接创建然后直接入账，除非你确认后端这等价于 create+confirm 且 audit 也完整。否则一律走 pending→confirm。

**二次确认卡片**（§10 末）：
```
💵 <b>确认收租</b>
物业：Bayshore
Unit：16B
金额：₱55,000
日期：2026-08-10
方式：Bank
[✅ 确认][❌ 取消]
```
只有点 `✅ 确认` 才写数据。

## 幂等与超时（§12 §13 §15，硬要求）

### A. 双击确认 → 只入账一次
- 每张确认卡生成时分配 `nonce`（8 字节 hex）并写入 callback_data + SQLite `idempotency_keys`.
- 点 `✅ 确认`：本地幂等查 `idempotency_keys`——
  - `done` → 直接回显上次结果（不动 API）
  - `in_flight` → 提示「处理中，请勿重复」并忽略
  - 无 → `in_flight` → 调 API → 成功 `done` 存 result → 失败 `failed` 可重试。
- nonce 也进 callback_data（如 `v1:icf:inc:<nonce>`），同卡的第二次点击直接忽略或刷新卡片。
- 即便本地没挡住，后端 confirm 非 pending → 409，Bot 捕获后当「已确认」刷新卡片，不吓用户。

### B. 网络超时后二次点击（§13，重点测试）
设计「不确定状态」处理：
```
try: ret = confirm()
except Timeout / 连接错误 / 5xx：
    # 不确定：查后端最终状态
    status = GET /incomes/{id}     # 用 income id / reference 反查
    if status in (confirmed, reversed): 卡片刷新为已确认/已冲销，绝不新增
    elif status == pending: 保持 pending，提示「处理中，请重试」，不重复确认
    else: 报错并给错误编号
```
- **绝对禁止** `except: 报「操作失败，没有修改数据」`，因为在超时时你可能已经写成功。
- 用 income id 作为 reference key 反查，避免重复调用 confirm。

### C. 旧按钮/过期 callback（§14）
- callback_data 里带 `ts`（卡片生成时间戳）或按 conversations TTL（15 分钟）。
- 点击旧按钮：先拿 conversations 状态 / 后端当前状态校验——
  - 账期过期 / income 已 confirmed / reversed / lease 已结束 → 回「⚠️ 此操作已经过期，请重新打开当前账期。」**绝不入账**。
  - `answerCallbackQuery` 提示，不执行写。
- decode 未知版本 / 非法 id → 忽略 + `answerCallbackQuery("已过期")`.
- 对「确认收租」旧卡：即使 callback_data 有效，**后端才是最终裁决**：`status==pending` 才 confirm；非 pending → 409 → 提示已过期/已处理。

### D. callback_data 长度（§15）
- `<=64 bytes`，版本化前缀 `v1:<action>:<entity>:<id>:<nonce>:<ts>`，全 ASCII+数字+冒号，无 JSON、无中文、无 base64。
- `keyboards.py` 单一 encode/decode 真源；decode 校验版本前缀。

## 权限 / RBAC（§16）
- 本轮角色源：复用 `~/Documents/Codex/pasay-pm` 外既有 `roles.json`（OWNER 5177241442 全权；SECRETARY 1083657401 录收入但**不能** confirm/finalize）。把 roles 复制进 `pasay-telegram-bot/pasay_bot/roles.py`（硬编码一份或读 assets，你来定，便于测试）。
- **UI 隐藏不是安全机制**：Bot 按角色显示/隐藏「确认」按钮；但**真正权限由后端 API enforcement**——即 Bot 用的 API key 的权限。若本轮只用单一 manager key，则 confirm 对 manager 及以上开放；reverse 仅 OWNER 触发且用 admin key 兜底（若无独立 admin key，reverse 本轮可仅 OWNER 可见且调用返回 403 时提示无权限）。
- 关键：`roles.py` 的判定必须能被单测覆盖（ag agent/manager/admin 各权限），并有 `permission_bypass` 测试证明「即使手工构造 callback_data rent:confirm:xxx，无权限者后端仍拒绝」。

## Renderer（§5/§21）
- 统一 `render/html.py` + `render/cards.py`，**所有消息都走 renderer**，不得散落 f-string 拼接（除 helper 内部）。
- html.escape 覆盖：property 名带 `&`、tenant 名带 `<Admin>`、地址 `5 > 3 Street`，不得 `can't parse entities`。
- 金额：Decimal → 千分位字符串；测试 ₱0、₱0.01、₱55,000、₱1,500,000、reverse 负显示。
- None / 空数据 / 空列表：友好 fallback（如「暂无数据」），不崩。
- message length <= 4096 字符；超长自动截断或分片（分页优先）。

## i18n（§20）
- 少量文案放 `render/i18n.py`（或 `render/i18n/zh.py`、`en.py`），`t("finance.title", locale)`。
- 中文完整；英文至少覆盖主菜单 + 状态。`locale` 由 roles.py 里 per-user 决定（OWNER=zh，SECRETARY=en，可配）。

## 测试（§21，硬要求，至少这些，禁止减少现有后端 102 个测试）
在 `pasay-telegram-bot/tests/` 新增（沿用仓库 pytest）：

必建（renderer/金额/转义/空/分页）：
- test_property_renderer_chinese
- test_property_long_address
- test_property_pagination
- test_finance_decimal
- test_zero_income
- test_large_amount
- test_html_escape
- test_empty_tenant
- test_no_overdue
- test_overdue_sort
- test_empty_properties
- test_message_length
- test_pagination
- test_reverse_display

必建（收租 callback/确认/幂等/超时/权限）：
- test_rent_callback
- test_rent_confirm
- test_duplicate_rent_callback        ★
- test_double_confirm                 ★
- test_expired_callback               ★
- test_invalid_callback
- test_agent_permission
- test_manager_permission
- test_admin_permission
- test_permission_bypass              ★
- test_backend_timeout_before_write
- test_backend_timeout_after_write    ★
- test_idempotency                    ★
- test_app.api_client 409→已确认语义

「★」为核心防御用例，必须真实断言（例如 test_backend_timeout_after_write：模拟 API confirm 实际成功但响应超时，断言再点确认不产生第二笔收入、不以「未修改数据」误导、最终刷新为已确认）。

## 完成标准/验收
1. `pytest pasay-telegram-bot/tests` 全绿；`pytest tests`（后端）仍 102 个全绿。
2. 无直接 DB 写（代码走 Passay API client）。
3. `python -m pasay_bot.main` 能启动、`getMe` 自检通过（可用测试 token 或 dry-run；**不要启动真实生产 polling**，本轮只本地验证，Hermes 实机测试由 orchestrator 在 Phase 4 用测试 token/dry-run 做）。
4. bin/start-native-bot.sh 写好但**不要真正部署/注册 launchd**——部署与切流由 orchestrator 在 Phase 4+ 处理。
5. 不要改现有后端 app/ 代码；不要改 DB schema；不要 commit（orchestrator 统一提交）。
6. 产出简短总结：实现清单 / 测试结果 / 你做的关键取舍 / 遗留项。

写完代码后运行 `git status` 确认只新增 `pasay-telegram-bot/` 与该阶段文档。
