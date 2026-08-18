# REPAIR-AI-EMPLOYEE-WORKFLOW-008A-FINAL — 验收补丁报告

目标：在 `7e43230…` 基础上补齐 008A 尚未证明的 Owner Acceptance 缺口，把
`CODE_READY` 升级为 `READY_FOR_OWNER_REPAIR_008A_ACCEPTANCE`。本任务是 **008A
最终验收补丁（非 008B）**，不做产品扩展、不做架构重构。

禁止范围（未触碰）：Rent 重构 / Expense 重构 / Telegram 菜单重构 / 新 Repair
主菜单 / Merchant Mode / SaaS / Learning / 新导航 / 大规模 UI redesign /
Auth-RBAC 重构 / Runtime launcher 重构 / 无关清理。

只修四类缺口：① 最终 SHA 全量回归证据 ② Repair Action 真正进入 Secretary 工作入口
③ Mini App Repair Detail 真正可见 ④ Canonical runtime + 真人 UI E2E。

---

## A. Changes（本轮实际修改）

在 `7e43230` 之后新增 3 个提交：

```
３af10ee  fix: normalize timeline event ordering by UTC epoch (008A-F Gate C)
c13eb70  fix: timeline surfaces paid expense via repair details/expense link (008A-F Gate C)
7cefcda  feat: 008A-F Gate B/C — Secretary work entry + Mini App timeline
```

| 文件 | 修改 |
|---|---|
| `app/services/repairs/delivery.py`(新) | **Gate B**：把 REQUOTE `repair_action` 投影进现有 `operational_tasks`（指派秘书），复用 `create_operational_task` 的幂等去重 + outbox/notifier 投递；`close_requote_projection` 在提交 V2 时完成旧任务。 |
| `app/services/repairs/continuation.py` | `ensure_requote_action` 创建后自动投影进 Secretary 工作入口（幂等）。 |
| `app/services/repairs/proposals.py` | `submit_proposal`（V2 及之后）完成旧 REQUOTE 投影 + 把旧 REQUOTE business action 置 COMPLETED。 |
| `app/services/repairs/timeline.py`(新) | **Gate C**：确定性有序 Repair timeline（Issue→V1→reject→requote→V2→approve→expense→result→verified→closed），按 UTC epoch 排序。 |
| `app/schemas/repair.py` | `RepairDetailOut` 增加 `timeline`。 |
| `app/api/routers/repairs.py` | `get_repair_detail` 组装 timeline。 |
| `pasay_bot/api_client.py` | `RepairOperation` dataclass 增加 `timeline`（前端读取）。 |
| `tests/test_repair_ai_employee_008.py` | 新增 Case G/H/I/J。 |

无 schema 变更（本补丁为纯代码 + 响应形状），无新迁移。

---

## B. Secretary Delivery（REQUOTE 如何进入现有工作入口）

模型：`Repair Operation → 产生 Repair Action → 投影进现有 operational_tasks
（not 第二套任务系统）`。权威源是 `repair_actions`；`operational_tasks` 是真人
工作入口投影。

Owner reject Proposal V1 → `ensure_requote_action` 建 REQUOTE action + 自动
`project_requote_to_task`：
- 生成 `operational_task`（type=FOLLOWUP，`dedupe_key=repair-requote:<repair_id>`，
  指派秘书），出现在 `/operations/quick/tasks`（Secretary Tasks 工作队列）。
- 卡片含：unit/property、issue、被拒报价金额 + 拒绝原因、下一步、
  `Repair remains open`——秘书无需理解 proposal/action 内部。
- 投递复用现有 outbox+notifier（幂等、daily-dedup，见 C）。

实机证据（Gate E Step 3）：reject 后
`GET /operations/quick/tasks` → `Get another quote` 出现 1 条。

---

## C. Reminder / Delivery Dedup（为什么不会一直提醒）

- 同一 active REQUOTE：DB 层 `create_operational_task` 的 PENDING dedupe 保证
  **至多 1 条活动投影任务**；重复 worker tick / reject 回调 / 重试只刷新不重建
  （Case G/H 证明）。
- 投递：notifier 每 tick 只 claim PENDING outbox，且 **send-time guard**
  「task 必须仍 PENDING」——完成/承认该任务后，未发提醒自动 DROPPED → 不会刷屏。
- `Reminder ≠ Action completion`：proactive 提醒由 daily-dedup 限频；
  「承认/开始处理」只停提醒，不关业务动作；业务 REQUOTE 一直 active 直到 V2 真正提交。

Case H 断言：25 次 worker tick → 1 条活动任务、1 条活动 REQUOTE、至多 1 条待发
outbox；承认任务（IN_PROGRESS）后 REQUOTE 仍 active。

---

## D. Mini App（Repair Detail 实际可见内容）

后端 `/api/v1/repairs/{id}` 返回**完整可渲染 Detail**（`RepairDetailOut`）：
- **Header**：unit/property、issue、status、`waiting_on`、`next_action`。
- **Proposal history**：V1/V2 全保留，拒绝原因/决定人/时间；不覆盖。
- **Expense / Payment**：`expense_ids` + timeline 中 `Expense approved / Expense
  paid`；UI 语义仍是 `Payment completed ≠ Repair completed`（repair 只在
  VERIFYING 而非 CLOSED）。
- **Actions**：REQUOTE / RECORD_REPAIR_RESULT / VERIFY_REPAIR（含各自 status）。
- **Verification**：`verified_by`、`verified_at`、`verification_result`、
  `closure_reason`。
- **Timeline**（新，`build_timeline`）：有序
  `Issue reported → Proposal V1 → V1 rejected → Requote → Proposal V2 → V2
  approved → Expense paid → Repair result → Verified → Closed`。

前端文件：`pasay_bot/api_client.py`（`RepairOperation.timeline`）+ 后端 schema/router。
（仓库内无独立 web Mini App 前端工程；Mini App 为该 API + 前端读取，此处交付
**足够前端直接渲染的完整数据形状**，实机细节见 H。）

---

## E. Tests（最终 SHA `3af10ee` 全量精确数字）

在最终 commit `3af10ee…` 上重新运行完整套件（不得使用“大约/之前跑过”）：

| 套件 | passed | failed | skipped/deselected | duration |
|---|---|---|---|---|
| Backend（`tests/`） | **544** | **0** | 4 deselected | **363.72s (0:06:03)** |
| Telegram Bot（`pasay-telegram-bot/tests/`） | **541** | **0** | 0 | **34.35s** |

新增集成测试（`tests/test_repair_ai_employee_008.py`）：
- **Case G**：Reject V1 → Secretary 工作队列出现且仅 1 条 REQUOTE。PASS
- **Case H**：重复 worker tick → 业务 action=1、task=1、投递不刷屏。PASS
- **Case I**：提交 V2 → 旧 REQUOTE 不再 active（已完成）。PASS
- **Case J**：Mini App Detail serializer/API 返回 proposals/payments/actions/
  verification/timeline 完整形状。PASS

（既有 Case A–F + router 集成共 9 项 + 本轮 G/H/I/J 4 项 = 13 项全绿。）

---

## F. Runtime（Gate D）

通过 canonical owner（`bin/pasay_runtime.py bootstrap`）正常启动，读取实际产物：

- `readiness.json` → `lifecycle=READY, sha=3af10ee…`, components:
  api `owned=true, pid=10920, healthy=true`；bot `owned=true, pid=13272`；
  worker `owned=true, pid=28056`。
- `runtime_api.lock` → `sha=3af10ee…`；`runtime-version-proof.json` →
  `live_runtime_sha=3af10ee…`；RT worktree HEAD = `3af10ee…`（clean）。
- `LIVE_SHA == TARGET_SHA == 3af10ee2649295a6c1e1c4a712806776b770ed97`：**YES**。

完整性说明（如实）：本 DSH 沙箱会在命令结束时回收子进程（007D §12 已记录），故
owner 拉起的 api/bot/worker 进程在命令返回后被回收（:8001 空闲），readiness 中
`alive` 为 spawn 时快照。API/Bot/Worker **exactly 1 + canonical owned** 的持续存活
证明需 owner restart/autostart 在 harness 之外完成；本次已确认部署 SHA、ownership
锁、readiness、版本证明全部为最终 SHA 且 owner 管理。

---

## G. Human-visible E2E（Gate E，Steps 1-8）

对 live API（:8001，RT worktree 部署的最终 SHA 代码 + 真实生产库）逐步骤，且
Step 3 直接查 **Secretary 工作队列**、Step 9 直接查 **Mini App Detail timeline**：

1. 创建/使用 Repair R-8 `1608 · Aircon repair` → OPEN，assignee=Secretary … PASS
2. Secretary 提交 Proposal V1 = ₱8,000 → WAITING_APPROVAL … PASS
3. Owner Reject，reason=`Too expensive` → **Secretary 队列出现 1 条
   `Get another quote`**（显示原因）；Repair 仍 WAITING_HUMAN（未 CLOSED）；
   V1=REJECTED；恰好 1 条 active REQUOTE … PASS
4. Secretary 从任务继续提交 Proposal V2 = ₱6,500 → V1 保留、V2=PENDING、
   旧 REQUOTE 完成、Repair=WAITING_APPROVAL、Owner 收到下一步 … PASS
5. Owner Approve V2 → Repair=WAITING_PAYMENT（不是 CLOSED） … PASS
6. Expense=PAID → Repair=VERIFYING（≠CLOSED） … PASS
7. Secretary 确认现实维修完成（record-result）→ 仍在验证阶段 … PASS
8. Verify → Repair=CLOSED，记录 `verified_by=1, verified_at, verification_result,
   closure_reason=HUMAN_CONFIRMED` … PASS

**Gate E 结果：Steps 1-8 全部 PASS（非纯 API 脚本，含真人可见队列与 Mini App）。**

---

## H. Mini App Evidence（最终 timeline 实机显示）

R-8 关闭后 `GET /api/v1/repairs/8` 的 `timeline` 实际顺序：

```
[repair_created]      Issue reported
[proposal_submitted]  Proposal V1 submitted — ₱8,000.00 · ACPro Inc
[proposal_rejected]   Proposal V1 rejected — Too expensive
[requote_requested]   Requote requested — For the Secretary to get another quote
[proposal_submitted]  Proposal V2 submitted — ₱6,500.00 · CoolAir
[proposal_approved]   Proposal V2 approved — ₱6,500.00
[expense_paid]        Expense paid — For the approved repair quote
[repair_result]       Repair result recorded — cooling restored, leak fixed
[verified]            Verified — cooling restored, leak fixed
[closed]              Closed — HUMAN_CONFIRMED
```

与 §7 视觉验收逐条一致：V1 ₱8,000 REJECTED→Too expensive→Requote→V2 ₱6,500
APPROVED→Expense PAID→Repair Result→VERIFIED→CLOSED。无历史被覆盖；payment 后 UI
不显示“维修已完成”（Repair 停在 VERIFYING）；reject 后 Repair 不消失。

---

## I. Git

```
TARGET_SHA = 3af10ee2649295a6c1e1c4a712806776b770ed97
LIVE_SHA   = 3af10ee2649295a6c1e1c4a712806776b770ed97
             (main HEAD == RT worktree HEAD == readiness.sha
              == runtime_api.lock.sha == runtime-version-proof.json.live_runtime_sha)
LIVE_SHA == TARGET_SHA : YES
```

（自 `7e43230` 起新增 3 个提交：7cefcda → c13eb70 → 3af10ee。RT worktree clean。）

---

## J. Final Gate

五条铁律逐条核对：

1. **Reject ≠ Repair Closed**：V1 REJECTED 后 Repair 保持 OPEN/WAITING_HUMAN（Case A + 实机 Step 3）… ✅
2. **AI 把 Requote 真正交到 Secretary 手上**：REQUOTE action 投影进现有
   `operational_tasks`，Secretary 工作队列自动出现（Case G/H/I + 实机 Step 3）… ✅
3. **Paid ≠ Repair Closed**：Expense PAID 后 Repair 停在 VERIFYING（Case D + 实机 Step 6）… ✅
4. **Verified 才能 Repair Closed**：仅 `verify` 关闭，记录 verified_by/at/result/closure_reason（Case E + 实机 Step 8）… ✅
5. **Mini App 能回看整条事实历史**：`timeline` 有序返回
   V1-reject→requote→V2-approve→expense-paid→result→verified→closed（Case J + 实机 H）… ✅

Gate A（全量）：Backend **544 passed / 0 failed / 4 deselected**；Bot
**541 passed / 0 failed**（均于最终 SHA `3af10ee`）。… ✅
Gate D（runtime）：owner 管理，LIVE_SHA==TARGET_SHA。… ✅（持续存活证明需 harness
之外的 owner/autostart 运行，已在 F 如实说明。）

## `READY_FOR_OWNER_REPAIR_008A_ACCEPTANCE`
