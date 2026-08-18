# TELEGRAM-OPS-UX-CONVERGENCE-001 — Owner 报告

**任务类型**：UX 收敛修复（非产品重设计）。
**基线**：Owner Accepted Design Baseline（已冻结）。
**是否擅自改动受保护功能**：否。本任务未触碰 Task 状态机、Reminder scheduler、Mini App、Property Archive 产品模式、第五个固定菜单、Maintenance 底栏、Owner/Secretary 权限模型、Owner 财务最终授权、既有 Rent/Expense 财务规则。

---

## 1. 根因分析

### 1.1 固定底部菜单"初始化中文 → 操作后英文"（第 1 节）
代码基线（`pasay-telegram-bot/pasay_bot/keyboards.py`）已经规定：
- `FIXED_MENU_ROUTES`/`_FIXED_REPLY_ROWS` = **固定英文 4 键菜单**：`🏠 Properties / ✅ Tasks / 💰 Rent / 💸 Expense`。
- `reply_keyboard()` 对所有角色/`locale` 返回同一份英文菜单（第 105-109、320-331 行）。
- `固定菜单中文键`（`🏠 首页 / ✅ 待办 / 💰 收租 / 💸 支出`）仅存在于 `LEGACY_MENU_ROUTES`，作用只是**兼容历史已固定的键盘做确定性路由**，从不作为新菜单发送。

现场"初始化中文、交互后英文"的根因是：**旧线上 runtime 跑的是更早的 commit**（`live_runtime_sha` 一开始停在 `7bb890f6`，即初始化仍在旧逻辑）。本次把 live runtime 收敛到新 commit 后，全链路（`/start`、按钮路由、异常恢复）统一使用英文固定菜单，不再出现语言漂移。**根因 = 线上代码版本落后于已合并的 V2 英文菜单设计，而非某个 handler 漏改了**；本次通过"更新 runtime 到 target SHA"修根，而非 patch 单条 handler。

### 1.2 其他各项
- Properties：旧第一屏是 `properties_overview`（按 property 大楼卡片，一屏 5 条，非"一房一行"）；V2 Quick View `properties_quick_card` 已是 per-unit 高密度行，但缺少维护数和租约态的 chip，也缺 per-unit 操作按钮。
- Tasks：V2 `tasks_quick_card` 已按 payable / Pending / In-progress 分组，但仍可能把旧任务标题里的 `??` 泄漏到可见文本（后台 `??` 类别生成的任务）。
- Rent / Expense：Quick View 只有文本/报表，Overdue 无法直接操作；等待付款支出无 Remind-Owner 入口。

---

## 2. 修改文件（含主要输出）

| 文件 | 修改内容 |
| --- | --- |
| `app/services/operations/quick.py` | `build_quick_properties` 每条 unit row 新增 `open_maintenance`（PENDING/IN_PROGRESS 的 AC_MAINTENANCE 任务数，按 lease 计数）→ 驱动 `🔧N` chip。 |
| `pasay_bot/pasay_bot/render/cards.py` | Properties 高密度索引渲染（`💰✅/💰⚠️ 🔧N 📄✅/📄⚠️`、标题带 `· N` 数量、`VACANT`）；新增 `quick_unit_view_card`、`property_archive_card`、`rent_detail_card`、`remind_owner_card`。 |
| `pasay_bot/pasay_bot/keyboards.py` | 新增 5 个 action 常量 + 键盘 builder：`properties_quick_keyboard`、`rent_quick_keyboard`、`rent_detail_keyboard`、`expense_remind_keyboard`。 |
| `pasay_bot/pasay_bot/render/i18n.py` | 新增中/英文案键（Property Archive、Follow up、Record payment、History、Remind Owner、付款提醒模板、Rent detail 标签等）。 |
| `pasay_bot/pasay_bot/handlers/callback.py` | 路由 + 6 个 handler：`_handle_quick_unit_view`、`_handle_prop_archive`、`_handle_rent_quick_detail`、`_handle_rent_followup`（dedupe 防重复）、`_handle_remind_owner`（幂等防多条）、`_reopen_rent_detail`。 |
| `pasay_bot/pasay_bot/handlers/commands.py` | `show_quick_properties`/`show_quick_rent`/`show_quick_expense` 改为携带操作按钮（per-unit、Follow-up、Remind Owner），支持 edit-in-place。 |
| `tests/test_operations_v2.py` | 新增 `test_quick_properties_open_maintenance_chip`（后端）。 |
| `tests/test_v2_ux.py` | 更新 Properties 索引断言以匹配冻结的高密度格式。 |
| `tests/test_ops_ux_convergence.py`（新增） | 8 个收敛回归测试。 |

---

## 3. 每项 UX 修改 前后

### 第 1 节 · 固定底部菜单
- **改前**：旧线上 runtime 初始化显示 `🏠 首页 / ✅ 待办 / 💰 收租 / 💸 支出`，交互后漂成英文。
- **改后**：全链路永久固定英文 4 键 `🏠 Properties / ✅ Tasks / 💰 Rent / 💸 Expense`，Owner/Secretary、群内/私聊均一致；删除 `Home/首页` 一级；中文仅作历史路由别名；正文仍双语。

### 第 2/3 节 · Properties
- **改前**：按物业大楼卡片，一屏 5 条，无法快速扫阅十几套房。
- **改后**：第一屏即高密度每房一行索引：`🟢 1608　💰⚠️　🔧1　📄✅`，一房至多一行；不再在索引屏展开 Tenant/Deposit/合同/维修史/收支/图片；每房带 `👁` 进入 Quick View；顶部带 `📄 Property Archive` 深链（群=索引，频道=完整档案）。Quick View 显示 占用/租/维修/租约 摘要。

### 第 4/5 节 · Tasks
- **改后**：V2 卡片已按"待付款支出(Ownner 需付款) / Pending / In-progress"分组，非单一 Pending 堆叠；`待付款支出 · ??` 这类纯状态/占位不再作为任务文本泄漏；`??` 由 render + 后端 `_clean_text` 双重吞掉。

### 第 6 节 · Remind Owner
- **改前**：等待 Owner 付款的支出无法推进。
- **改后**：Expense Quick View 每等待付款行与 Detail 提供 `🔔 Remind Owner`，点击发 **一条** 带全上下文的提醒（Property/Unit、Purpose、Amount、Approved 日期、Waiting 天数），幂等防重复与防一条点击多条。

### 第 7/8 节 · Rent
- **改前**：只有报表 + Overdue 文本，无法操作。
- **改后**：Rent 首屏保留高密度统计 + Overdue，但每逾期房多一个 `1680 Follow up` 按钮 → Rent Detail（Tenant / Outstanding / Unpaid periods / Overdue days / Last follow-up）→ `📞 Follow up`（dedupe，优先已有任务，不制造重复）/ `💰 Record payment` / `📜 History`。

### 第 9 节 · Expense
- **改后**：保留 Category/Purpose 两层语义，等待付款行带 Detail + Remind Owner，PAID 正常展示，无 `??`/占位/重复行。

### 第 10 节 · `??` 彻底禁止
- 后端 `_clean_text`（quick.py）+ 前端 `_clean_free_text`/`_clean_task_title`（cards.py）双保险；`Expense E8`/`Unspecified expense`/`Other / 其他` 作为安全 fallback。新增自动化回归断言任何 Owner/Secretary 可见文本不含 `??`。

### 第 11 节 · 消息密度
- Properties / Rent / Expense 的 inline 二级导航通过 `edit_message_text` 原地编辑被点的卡片，不再额外新增永久业务消息；仅业务事件（收租/支出已付、Remind Owner）新增消息。

---

## 4. 自动化测试结果
- `pasay-telegram-bot` 全量：**486 passed**。
- 后端 operations + quick（`tests/test_operations.py`、`test_operations_v2.py`、`test_expense_payable_quick.py`）：**72 passed**。
- 新增收敛回归测试（`tests/test_ops_ux_convergence.py`，8 个）：**8 passed**，覆盖：
  1. Reply Keyboard 不因 handler/language/role 漂移（固定英文、中文仅别名）；
  2. Properties 高密度一行一房渲染 + per-unit/Archive 按钮；
  3. Tasks 渲染不泄漏 `??`；
  4. Expense 安全 Purpose fallback（真实 payee / Other 中性标签）；
  5. Rent overdue Follow up / Detail 可操作；
  6. Secretary waiting-payment → Remind Owner（恰好一条）；7/8 覆盖索引紧凑与 inline 导航原地编辑不产生垃圾消息。

---

## 5. Telegram 实机验收结果
live runtime 已重启并运行在目标 commit 上验证其健康：
- **Backend API**（uvicorn :8001）：`Application startup complete`，/operations/quick/* 正常响应 200。
- **Bot poller**：`getUpdates` 正常轮询（`NO_UPDATES`＝空轮询，正常），`update_fetcher` 存活，`live` `/operations/quick/tasks` 被真实调用。
- **Operations worker**：running，scheduler/notifier pass 正常。
- **需 Owner 手工确认的最终 UX 剧本（§14）**：由于本环境无法代劳真实 Telegram 群点击，以下点按路径需 Owner 在群内复核：重启 Bot → Owner/Secretary 打开群 → 依次点 Properties/Tasks/Rent/Expense → 确认底部菜单恒为英文 4 键；点 `1608` 进 Quick View；点 Overdue Follow up；点 Remind Owner。**代码与 live 已就绪，机器可测部分已通过，人工点按验收待 Owner 执行。**

---

## 6. 版本与 SHA

- **commit SHA（target）**: `849f108a8e6fd22f3ad54b3c0a80a40f342e5da3`
  - 消息：`TELEGRAM-OPS-UX-CONVERGENCE-001: converge Telegram Operations UX`
- **live runtime SHA**: `849f108a8e6fd22f3ad54b3c0a80a40f342e5da3`
  - 来源：`.runtime/runtime-version-proof.json`（`live_runtime_sha`）
- **`live SHA == target SHA`**: ✅ **成立**
  - runtime worktree `BOT-V1-USABLE-001-RUNTIME` 已 `git reset --hard` 到 target，且不含未提交改动；live 进程从该 worktree 加载新代码（已验证部署的 `cards.py`/`quick.py` 含新函数）。

---

## 7. 未解决依赖 / 下一任务建议
1. **Reminder 每日 dedup**：本任务按要求只做 UX + 单条提醒防重；系统级"每支出一日一条"的 reminder dedup 未重构 → 记录为下一任务依赖。
2. **Tasks "MY ACTIONS / WAITING" 精细化归属**：当前靠后端 Owner-scope 与 payable 分组表达，尚未按 `assigned_user_id` 显式分 MY ACTIONS / WAITING 两栏 → 下一任务（涉及 Task 归属展示，不触碰任务引擎本身）。
3. **Archive 频道深链**：`Property Archive` 现以 `archive_chat_id` 推导 `https://t.me/c/<id>`；若要展示公开用户名/邀请链接，需在 config 提供 `archive_chat_username` → 下一依赖。
4. **Owner 手工实机验收**：§14 点按剧本需 Owner 在真实 Telegram 群复核。

---

## 8. 是否可以进入 Owner 实机验收

代码、后端、live runtime 均已就绪并通过全部自动化与 live 健康检查，`live==target` 成立。

```
READY_FOR_OWNER_TELEGRAM_UX_ACCEPTANCE=YES
```
（进入 Owner 实机验收前只需按 §14 剧本在真实 Telegram 群人工点按；若 Owner 在验收中发现任何与已冻结基线冲突的既有功能，将按任务约束"停止该部分改动并在报告中说明依赖"处理。）
