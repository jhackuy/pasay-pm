# SLICE2-RENT-003 — Rent Status NL Queries（租金状态自然语言查询，只读切片）

## 1. Existing / Missing / Minimal Change

### Existing（已有，文件名）
- Overdue 数据与渲染：`app/api/routers/reports.py`（`/reports/overdue-rents`，OverdueRent：unit/lease/tenant/overdue_periods/total_outstanding/overdue_days）、`app/schemas/reports.py`、`pasay_bot/api_client.py::OverdueRent`、`pasay_bot/handlers/commands.py::build_overdue_page / show_overdue`、`pasay_bot/render/cards.py::overdue_list / overdue_block`
- 按钮路由：`pasay_bot/handlers/nl_bridge.py::route_for_text`（"逾期/欠租/overdue" → overdue 页）；收租登记排除逻辑 `is_rent_payment_statement` / `_QUERY_SUFFIX`
- income/payment 查询能力：`api.list_incomes()`、`api.get_leases()`、`api.get_tenants()`、`api.get_units()`、`api.get_properties()`、`commands.py::_period_covered`（confirmed income 按 description `rent YYYY-MM` 或 received-date 月覆盖）
- 读权限：`pasay_bot/roles.py`（`PERMISSION_READ` / `has_read_permission`，OWNER/SECRETARY）；i18n：`pasay_bot/render/i18n.py`

### Missing（真正缺的）
- 单房源/单租客状态查询（"1608 交了没有" / "John 交了吗"）：无
- "这个月谁还没交"的会话式答案（现有 overdue 页是分页列表，不直接回答）：无
- 查询意图识别（此类句子现落 unknown）：无
- 按当前账期过滤的已交/未交状态：无

### Minimal Change（最小路径）
- 只改 bot 侧 3 个文件 + 新增 1 个测试文件（见第 2 节）
- **后端无缺口，未新增任何端点**：三类查询全部复用现有 GET（units/leases/tenants/incomes/overdue-rents/properties），月覆盖判断复用 `commands._period_covered`（与收租收集列表同一规则）
- 未改表结构、无 migration、未动 `app/api/routers/income.py`、未重实现任何财务查询/报表/RBAC/audit

## 2. 修改文件列表
- 修改 `pasay-telegram-bot/pasay_bot/handlers/nl_bridge.py`（确定性 detector + 只读 handler，在 `is_rent_payment_statement` 之后、`route_for_text` 之前拦截）
- 修改 `pasay-telegram-bot/pasay_bot/render/cards.py`（`rent_status_card` / `tenant_candidates_card` / `unpaid_list_card`）
- 修改 `pasay-telegram-bot/pasay_bot/render/i18n.py`（zh/en 各 13 个 `rent_status.*` 键，两侧一致）
- 新增 `pasay-telegram-bot/tests/test_rent_status_nl.py`（20 个测试）

## 3. 查询识别规则（正例/反例）
- Who-paid：zh「这个月谁还没交 / 谁还没交 / 谁没交 / 还没交房租的 / 谁没交租 / 还没交租金」；en `who hasn't paid` / `who has not paid` / `who didn't pay` / `unpaid this month`。反例：`收租`、`1608`、`John`、`财务`、`逾期` 均不命中（保持原按钮路由）。
- 单房：`1608 交了没有 / 交了没 / 交了吗 / 还没交`、`还欠多少 / 欠多少`；en `has 1608 paid?`、`how much does 1608 owe?`、`does 1608 owe?`。按 unit number 精确匹配（大小写不敏感）。
- 按租客：`John 交了吗 / 还欠多少`；en `did John pay?`、`how much does John owe?`。按租客全名/名字 word-boundary 匹配 active lease。
- 反例/不误判：以上查询 `is_rent_payment_statement` 全部为 False；`1608租金收到了`（登记语句）在 detector 之前被拦截，永远不会落到查询路径；`1608` 无查询动词 → unknown。

## 4. 数据链路（端点/字段 → 答案）
- 「这个月谁还没交」→ `GET /reports/overdue-rents` → 过滤 `overdue_periods[].month == 当前月` → `unpaid_list_card`（复用 overdue_block：unit/tenant/欠款/到期日/逾期天数）；物业名来自 `GET /units` + `GET /properties`（nice-to-have）。
- 「1608 交了没有 / 还欠多少」→ `GET /units` 精确匹配 unit_number → `GET /leases` 取 active lease → `GET /incomes` + `_period_covered` 判已交/未交（仅 confirmed）→ `GET /reports/overdue-rents`（该 lease 的 total_outstanding / overdue_days / overdue_months）→ `GET /tenants`、`GET /properties` 补名称 → `rent_status_card`。
- 「John 交了吗」→ `GET /tenants` word-boundary 匹配 → 每个 active lease 走同一状态链 → 单候选出 `rent_status_card`，多候选出 `tenant_candidates_card`（只读候选，不自动选）。

## 5. RBAC 处理
- 所有查询入口先过 `has_read_permission(role)`（OWNER/SECRETARY 为 true）；未知用户直接回 `common.no_permission`，且零 API 调用（测试断言）
- 后端继续由既有 `manager_or_admin` 保护所有被复用 GET（agent 403 行为未改、未新增端点，故不重复加后端测试）

## 6. 测试结果
- 新增：`pasay-telegram-bot` 下 `env -u PYTHONPATH .venv/bin/python -m pytest tests/test_rent_status_nl.py -q` → **20 passed**
- 回归：`env -u PYTHONPATH .venv/bin/python -m pytest tests -q` → **229 passed**（含原 rent NL / render / UX / income / idempotency 等全量 bot 测试）

## 7. 静态检查结果
- `PYTHONPYCACHEPREFIX=/private/tmp/... python3 -m compileall -q pasay-telegram-bot/pasay_bot app` → exit 0（bot + backend 语法/导入通过）
- i18n：zh/en 新键集合一致（13/13）；既有漂移 `en-only copilot.ready` 为改动前已存在，未触碰
- 沙箱无 ruff/flake8（同 SLICE2-RENT-002 记录），以测试 + compileall 作为静态证据

## 8. 已知风险/TODO
- `_period_covered` 复用收租收集列表的同一规则（pending 未确认计为未交）；当前月未到 due date 时 overdue-rents 不含该月 → 「谁还没交」返回空，与 overdue 页口径一致
- 不带查询标记的简写（"1608 交了" 无 吗/没有；en "1608 paid?" 无 has/did）暂不识别，属本卡范围外
- 多租客候选只读列出，选择器属后续卡片（本卡明确不做）
- 后端 PostgreSQL 全量回归与 Telegram 实机由 Windows/真实环境执行（Mac 边界）

## 9. HEAD
`1ebaa8504d9b503ffa9d781a0d0826379744870c`（`feature/telegram-ui-v2`）——本卡改动未提交（受任务约束不执行 git commit）。

## 10. workspace 状态
`feature/telegram-ui-v2`：3 个修改文件 + 1 个新增测试文件 + 本报告文件，全部为本卡改动，无无关变更。建议提交 message 前缀 `SLICE2-RENT-003:`。
