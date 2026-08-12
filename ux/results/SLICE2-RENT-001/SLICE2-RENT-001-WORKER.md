# SLICE2-RENT-001 — Entry B 自然语言收租（exact payment）第一闭环

## Reality Check（三块）

### Existing（已有，文件名）
- 财务模型：`app/models/financial.py`（Income 已含 lease_id / amount / received_date / payment_method / idempotency_key / status / description / confirmed_by/at）
- 租约与账期数学：`app/models/lease.py`、`app/services/operations/rent_math.py`（lease_periods / covered_periods / month_from_description）
- Income 创建 + Owner-only 确认 + 幂等：`app/api/routers/income.py`（idempotency_key 唯一索引、原子 write、audit）、`app/api/deps.py::owner_subject_only`
- Audit：`app/services/audit.py`、`app/models/audit_log.py`（confirm / create audit 已有）
- Bot 收租流程 + 确认卡 + 幂等 guard：`pasay_bot/handlers/callback.py`（_confirm_rent_entry / _confirm_income）、`pasay_bot/state/idempotency.py`、`pasay_bot/keyboards.py`
- NL 入口：`pasay_bot/handlers/nl_bridge.py`、`pasay_bot/handlers/conversation.py`
- 渲染与 i18n：`pasay_bot/render/cards.py`、`pasay_bot/render/i18n.py`、`pasay_bot/render/html.py`

### Missing（真正缺的）
- 文本/金额 → open receivable 的匹配服务（含 confidence 分级）与 API：新增 `app/services/payment_match.py`、`app/api/routers/payments.py`、`app/schemas/payment_match.py`
- Bot“收到租金”自然语言入口 + 确认卡 + duplicate 友好提示：`pasay_bot/handlers/nl_bridge.py`、`cards.py`、`keyboards.py`、`i18n.py`
- 确认后的余额展示（余额：₱0）与 period 归属 description：`callback.py`（description 改用 payload.period）

### Minimal Change（最小路径）
- 新增 3 个后端文件 + 1 个 schema 文件 + 注册 router（`app/main.py` 两行）
- Bot 改 5 个文件（nl_bridge / callback / cards / keyboards / i18n）+ api_client（RentMatch dataclass + match_rent_payment）
- **未改**：Income 模型/迁移（无 migration，描述字段承载 period）、confirm/RBAC/幂等链路（原样复用）、菜单/待办结构

## 修改文件清单
- 新增：`app/services/payment_match.py`、`app/api/routers/payments.py`、`app/schemas/payment_match.py`
- 修改：`app/main.py`
- Bot 新增：`pasay-telegram-bot/tests/test_rent_nl.py`
- Bot 修改：`api_client.py`、`handlers/nl_bridge.py`、`handlers/callback.py`、`render/cards.py`、`render/i18n.py`、`keyboards.py`、`tests/conftest.py`
- 测试新增：`tests/test_payment_match.py`

## 测试结果
- Bot 完整套件：**198 passed**（含新增 10 个 Entry B 测试：NL 命中、确认卡、确认幂等 + 原地 mutation、duplicate 友好、Secretary 无按钮、ambiguous / none / pending）
- 后端匹配服务 DB-free 单测：**15 passed**（HIGH 唯一匹配 / 重复识别 / pending / ambiguous / 金额不一致 / 日期与账期解析）
- 后端 API 集成测试：新增 2 个（exact high + duplicate / RBAC），依赖 PostgreSQL 测试库；本沙箱无法连接 DB（socket 被禁、shmget 被禁），随真实环境全量后端套件运行（完整套件可收集 365 tests）
- 既有财务幂等 / audit / RBAC 测试未改动：`test_financial_idempotency.py`（confirm x10 幂等）、`test_audit.py`（confirm audit）、`test_income_owner_policy_v13.py`、`test_handlers.py`（double_confirm / secretary 无确认按钮）保持原样

## Telegram 实机验证状态
本环境（Mac 开发机沙箱）无法实机：沙箱禁止本地 socket/网络连接（PostgreSQL 不可达，真实 API 无法启动），且不可读取 .env / bot token。由 Windows Fugui 在真实 Owner Telegram 上验收：Test 1 exact、Test 5 duplicate、Test 8 cleanliness。

## 剩余 UX 问题（后续卡片）
- partial / overpayment / ambiguous（多账单选择）/ correction / 查询：本卡只做 exact + duplicate，ambiguous 仅给简短人话列表，不做选择卡
- 期初拖欠多月的租客（>1 个未结账期）暂不自动出卡
- Secretary 的财务入账权限流（create pending → Owner confirm 的完整角色闭环）留待后续
