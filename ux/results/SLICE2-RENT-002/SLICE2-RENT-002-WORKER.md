# SLICE2-RENT-002 — Secretary Register → Owner Confirm（真实运营闭环）

## 1. Existing / Missing / Minimal Change

### Existing（已有，文件名）
- Exact payment matching（read-only，confidence 分级，pending/duplicate 分支）：`app/services/payment_match.py`、`app/api/routers/payments.py`、`app/schemas/payment_match.py`
- Income pending create + idempotency_key + Owner-only confirm：`app/api/routers/income.py`、`app/api/deps.py::manager_or_admin / owner_subject_only`
- RBAC / native-bot human-subject 绑定 / audit provenance：`app/api/deps.py`、`app/services/audit.py`、`app/models/identity.py`
- Bot NL 入口 + Entry B 卡片 + confirm 幂等 guard：`pasay_bot/handlers/nl_bridge.py`、`pasay_bot/handlers/callback.py`（_confirm_income）、`pasay_bot/state/idempotency.py`、`pasay_bot/keyboards.py`
- Telegram canonical identity + role mapping：`pasay_bot/roles.py`（TELEGRAM_USER_ID_TO_ROLE）
- 多语言：`pasay_bot/render/i18n.py`；卡片：`pasay_bot/render/cards.py`
- Owner Telegram routing（chat_id == user id 私聊）、duplicate/idempotency、audit actor：以上链路已具备（见上）

### Missing（真正缺的）
- Secretary 一句话英文报收租 → 自动登记 pending income：缺（nl_bridge 对 Secretary 只显示无按钮卡片）
- Owner 私聊中文确认卡 + 「有问题」只读状态提示：缺
- Secretary 重复报告时（pending / confirmed）的英文人话回复：缺（Owner 已有 duplicate/pending 分支，Secretary 未分流）
- 一句话登记的重放幂等键（同一笔付款共享一个 key）：缺
- 现有 matcher 的 unit hint 容错：句尾标点（"Received rent for 1608."）会把 "1608." 当 token，导致匹配不到——本卡修复（不是重写）

### Minimal Change（最小路径）
- **不动**：Income 模型 / 迁移 / confirm / reverse / RBAC / audit / 幂等主链路 / outbox / notifier——全部原样复用
- 后端仅改 1 个文件：`app/services/payment_match.py`（unit hint 标点容错，2 个函数微调）
- Bot 改 7 个文件 + 新增 1 个测试文件（见修改清单）；无新 migration、无新财务模型
- 未实现：Partial / Overpayment / 多候选选择器 / correction / 查询 / Maintenance / Renewal（超出本卡范围）

## 2. 修改文件列表

- 修改 `app/services/payment_match.py`（unit hint 标点容错）
- 修改 `pasay-telegram-bot/pasay_bot/roles.py`（telegram_id_for_role 反向查找）
- 修改 `pasay-telegram-bot/pasay_bot/keyboards.py`（ACTION_ISSUE + secretary_registered_keyboard）
- 修改 `pasay-telegram-bot/pasay_bot/render/i18n.py`（中英新文案）
- 修改 `pasay-telegram-bot/pasay_bot/render/cards.py`（5 个新卡片函数）
- 修改 `pasay-telegram-bot/pasay_bot/handlers/nl_bridge.py`（Secretary register 闭环）
- 修改 `pasay-telegram-bot/pasay_bot/handlers/callback.py`（_handle_issue + 终态卡渲染）
- 修改 `pasay-telegram-bot/tests/conftest.py`（fake matcher 同步标点容错）
- 修改 `pasay-telegram-bot/tests/test_rent_nl.py`（Secretary 断言改为新闭环）
- 修改 `pasay-telegram-bot/tests/test_roles.py`（反向查找单测）
- 修改 `tests/test_payment_match.py`（英文句尾标点纯单测）
- 新增 `pasay-telegram-bot/tests/test_rent_secretary.py`（10 个闭环测试）

## 3. Secretary → pending 数据链

`"Received rent for 1608."` → `is_rent_payment_statement` → `POST /payments/match`（manager 可读）→ 唯一 HIGH exact open → 幂等键 `ik:sec:{lease_id}:{period}:{received_date}:{amount}` → `POST /incomes`（status=pending，description=`rent YYYY-MM`，payment_method=Secretary 默认 Bank）→ 后端 `created_by`=Secretary canonical user（native-bot 凭证 + X-Telegram-User-Id 绑定 human subject）→ Secretary 英文回复（Rent payment matched … Sent to Owner for confirmation.）。

## 4. pending → Owner confirm 数据链

登记成功后 Bot 向 Owner 私聊（chat_id == 5177241442）发送中文卡：秘书登记了一笔租金 / 1608 · 8月租金 / 应收·收到 ₱70,000 / 登记人：Secretary / ✓ 金额一致 / ✓ 唯一未结账单，按钮 [✓ 确认入账][有问题]。

- [✓ 确认入账]：callback `v1:cnf:inc:{income_id}:{nonce}:{ts}` → `_confirm_income` → `POST /incomes/{id}/confirm`（X-Telegram-User-Id=Owner）→ `owner_subject_only` 校验 → confirmed → 原消息原地 mutation 为终态卡（租金已入账 / 1608 · 8月租金 / ₱70,000 / 余额：₱0 / 登记：Secretary），确认按钮消失、撤销按钮保留。
- [有问题]：`v1:iss:inc:{income_id}` → `_handle_issue`（只读 GET + 友好提示，无任何财务写）。

## 5. Owner 通知方式

Bot 本地直接向 Owner 私聊发卡（`telegram_id_for_role(Role.OWNER)` 反向查表，chat_id == user id）。未改后端 outbox/notifier（其当前只发纯文本、无 inline keyboard）。

## 6. RBAC 处理

- Secretary（manager）可调 `/payments/match`、可 `POST /incomes` 创建 pending（已有 `manager_or_admin`）；Bot 侧 `PERMISSION_RENT_ENTRY` 前置校验。
- Secretary 无 `PERMISSION_RENT_CONFIRM`：bot 在 `_confirm_income` 首行拒绝（测试断言零 API 调用）；后端 `owner_subject_only` 对非 Owner human subject 返回 403（`tests/test_income_owner_policy_v13.py` 已有覆盖，未重复）。
- [有问题] 也限定 Owner（防御手搓 callback）。

## 7. Duplicate 处理

- 情况 A（pending 未确认）：matcher 返回 pending → Secretary 英文 “This payment is already waiting for Owner confirmation.”，零写入、不重复通知 Owner。
- 情况 B（已确认）：matcher 返回 duplicate → Secretary 英文 “This rent payment has already been recorded and confirmed.”，零写入。
- 情况 C（update/callback 重放）：一句话登记使用确定性业务键 + bot guard；同一条消息重放/并发 → 第二次直接回 already-waiting，不创建第二条 pending、不重发 Owner 卡；confirm 重放 → guard done，仅一次 confirm 调用。任何路径都不暴露 409 / IntegrityError / idempotency_key / traceback。

## 8. audit actor 设计

复用现有 audit：create 的 `actor_id`/subject = Secretary canonical user（native-bot 凭证解析出的 human subject，channel=telegram）；confirm 的 actor = Owner。未改 audit 代码。

## 9. 静态检查结果

- Bot 全量：`pasay-telegram-bot` **208 passed**（含新增 10 个 Secretary 闭环测试）
- 后端匹配 DB-free：**16 passed**（含新增英文标点测试）；2 个 endpoint 测试需 PostgreSQL，按 Mac 边界不跑
- `python -m compileall`（bot + backend）：通过，无语法/导入错误
- 沙箱无 ruff/flake8 可用；测试 + compileall 作为静态证据

## 10. 已知风险/TODO

- Owner 卡 conversation 上下文 TTL 15 分钟且每 chat 单条：超时后 confirm 仍成功但终态卡回退通用成功卡（无余额/登记人行）；不破坏财务正确性。
- 同一付款若两次收到的 received_date 不同，确定性键不同；顺序重复由 matcher pending 分支兜底（不重复建），仅极端并发窗口依赖后端键（现有行为，未扩大范围）。
- Secretary 登记使用其默认付款方式（Bank），零 re-entry；后续如需「登记时附截图/凭证」属另一卡片。
- Telegram 实机体验、PostgreSQL 全量集成由 Windows/真实环境验收（Mac 边界）。

## 11. HEAD

`bfbd92a`（SLICE2-RENT-001UX，feature/telegram-ui-v2）——本卡改动未提交（受任务约束不执行 git commit）。

## 12. workspace 状态

`feature/telegram-ui-v2`，working tree 干净基线之上：11 个修改文件 + 1 个新增测试文件（见第 2 节），全部为本次改动，无无关变更。建议提交 message 前缀 `SLICE2-RENT-002:`。
