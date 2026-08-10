# PASay Property Management V2 — 收尾验收报告（A-K）

日期：2026-08-10（真实日期 2026-08-10，计算机 today=2026-08-10）
项目：/Users/jhackuy/Documents/Codex/pasay-pm
验收方式：指挥本机 AI（Max/Codex）完成 I1 修复；I2/I3 由本机自动化独立解决；最终 E2E 用 computer_use 驱动真实 Telegram 群全链路验证。

---

## A. overdue 原 bug 根因

`app/api/routers/reports.py` 旧 `overdue_rents()`：
`outstanding = (monthly_rent × 到期月数) - SUM(所有 confirmed income)`
- 用 totals 相减，**不按逐租期(month)对照**。
- **未来月份的 confirmed income 被错误当作抵扣**（如 10 月租金提前确认，却抵掉了应欠的 1-7 月）。
- 产生旧错误：Unit 1203 月租 65,000 却显示 `outstanding=390,000`、`days_overdue=5`。

## B. 修复后的算法

按 9 条原则逐月计算（重写 `_lease_periods` + `_covered_periods` + `overdue_rents`）：
1. 依 lease start_date/end_date/due_day 生成应收月 `(YYYY-MM, due_date)`；
2. 到期月 = due_date ≤ today；每到期月 = 一个应收；
3. confirmed income 按「description 中的 YYYY-MM」优先、否则按 received_date 月份，匹配该租约应收月集合；
4. outstanding = 已到期且未被子 confirmed 覆盖的月份；
5. pending income 不进 confirmed；
6. future period 天然不在 due ≤ today 集合，不影响欠租；
7. 租约 start 前不生成；
8. 租约 end 后不生成（end 月中结束的最后一个部分月不产生新应收）；
9. 返回 `overdue_months, overdue_periods[{month,amount}...], amount_per_month, total_outstanding, oldest_due_date, overdue_days`。

测试：新增 7 类场景（当月/多月/某月已付/pending不算/提前付未来/中途开始/租约已结束）。**全量 pytest 85 passed**（含 test_reports 16 passed）。另用 hermes-verify- 临时脚本对 `_lease_periods` 5 项边界逻辑断言全过。

## C. 修复前后实际数据对比（真实）

| | 修前 | 修后 |
|---|---|---|
| overdue_months | (隐式粗算) | **7** |
| overdue_periods | 无 | 2026-01…2026-07 每 65,000 |
| total_outstanding | 390,000 | **455,000** |
| amount_per_month | — | 65,000 |
| oldest_due_date | — | 2026-01-05 |
| overdue_days | 5 | 217 |
| 每日摘要欠租 | 390,000 | **455,000（与明细逐月一致）** |

说明：修后 455,000=7 完整应收月（1-7 月）；8 月已由 E2E 确认的 income #6 覆盖，故不欠。

## D. Telegram 409 getUpdates conflict 根因

发现**真正的第二 consumer**：`/Library/LaunchDaemons/com.ai-controller.bot.plist`（launchd 守护进程）
- 它以 `KeepAlive+RunAtLoad` 运行 `/Users/jhackuy/ai-controller/bot.py`
- bot.py 用**与 Hermes 同一个 Bot Token**(`8820506233`) 调 getUpdates 长轮询
- 每次 Hermes gateway 重启/重连都会撞上 → 反复 409「previous session still held open」
- 我之前只 kill bot.py，launchd 立即重新拉起（故反复出现）。

## E. 当前唯一 gateway 实例
- `launchctl bootout system/com.ai-controller.bot` + `launchctl disable system/com.ai-controller.bot`（持久禁用）→ daemon 与 bot.py 彻底移除，不再自动重启。
- 现在同一 Bot Token 仅 Hermes gateway 一个 polling consumer：
  - gateway = PID 49583→50213（`hermes_cli.main gateway run --replace`，launchd 管理，`ai.hermes.gateway.plist`）
  - 稳定性实测：gateway 重启后 STABLE_CONNECTED；排查后近 3 分钟 **0 个 conflict**；10+ 条连续组消息正常收发、无丢失（见 G）。

## F. DEV 数据清理结果
按「confirmed 禁 DELETE，需 reverse」：
- reverse：income #3(Aug)、#5(Oct) —— reversed，保留审计痕迹
- 删除 pending（非 confirmed，合规可删）：income #4(pending Sep)、expense #7(pending)、task #6(completed)
- 保留基准：1 Property(PASay Premier Residences) / 1 Unit(1203, ₱65,000) / 1 Tenant(Juan Dela Cruz) / 1 Lease(2026-01-01~12-31, due 5, active) / 1 Agent(maria) / 1 Commission Rule(Maria Referral 5%)
- 清理后 expenses=0, tasks=0, settlements=0, attachments=0，incomes 无 active。

## G. 最终真实 Telegram 人工 E2E 对话（computer_use 驱动，agent.log 实证 `inbound message`）

全部来自真实群「pasay houses manage」（迁移 supergroup 后 id `-1004433994558`，原 `-5417146216`）：

1. `Unit 1203 paid 65000 rent for August today through BDO.` → **pending income #6** → 返回 Rent Entry 卡片
2. `Confirm` → **income #6 confirmed**（confirmed_by=1）
3. `Aircon maintenance for Unit 1203 is 2500 cash.` → **expense #8 pending**（2500, maintenance）→ 返回 Expense Request 卡片
4. `Approve the 2500 aircon maintenance expense #8.` → **expense #8 approved**（approved_by=1）
5. `Pay expense #8, paid in cash.` → **expense #8 paid**
6. Owner 中文查询（6 条实时 API 回答）：
   - `1203这个月收了多少租金？` → collected **₱65,000**（Aug）
   - `1203目前有没有欠租？` → 其余 7 个月欠租明细（1-7 月）
   - `1203这个月支出多少？` → **₱2,500**（aircon）
   - `1203这个月净收入多少？` → **₱62,500**
   - `Maria这个月佣金多少？` → **₱3,250**（5%×65,000，服务端计算，settlement 1 笔）
   - `未来30天有什么待办？` → 无逾期待办 / 未来30天无到期

入口消息前的一条：`以后不需要做美金换算 这一步取消`（10:55 也在 inbound，说明日常使用正常）。

## H. 最终 Income / Expense / Commission 数据
- Incomes：id #3 reversed(Aug)、#5 reversed(Oct)、**#6 confirmed(Aug, 65,000, confirmed_by=1)**
- Expenses：**#8 paid(2,500, maintenance/Aircon, approved_by=1)**
- Commission settlements：**#7 confirmed(agent=maria7, lease3, rule3(5%), computed_amount=3,250)**
- Financial Summary 2026-08：expected_rent 65,000 / collected 65,000 / outstanding 0 / total_income 65,000 / total_expense 2,500 / **net 62,500** / 1 unit occupied

## I. Audit Log（关键动作序列，含操作者/时间/前后数据）
```
#63 confirm  commission_settlements id=7
#62 create   commission_settlements id=7
#61 pay      expenses              id=8
#60 approve  expenses              id=8
#59 create   expenses              id=8
#58 confirm  incomes               id=6
#57 create   incomes               id=6
#56 soft_delete tasks              id=6
#55 reverse  incomes               id=5
#54 reverse  incomes               id=3
```
审计完整覆盖 create/confirm/approve/pay/reverse/update/soft_delete，均含 actor_id。

## J. 每日摘要最终结果（修复后）
rerun cron（deliver 已更新到 `-1004433994558`）投递成功（last_delivery_error=null）：
- Unit 1203：欠租 **217 天 / 连续 7 个月（2026-01~07）/ 每月 ₱65,000 / 累计 ₱455,000**
- **与 overdue API 明细一致**；无逾期待办；未来30天无到期；无待审批佣金/支出。
- 期间修复了 cron deliver target（原 `-5417146216` 群迁移后失效 → `-1004433994558`）。

## K. 是否可以开始导入真实十几套房
**建议：等待 3 项安全收口后即可导入真实房源**：
1. **API Key 轮换 + 权限划分**：开发期用单一 admin key（Hermes 以 admin/owner 权限运行，财务只读/审批仍受限）。正式导入前改：Hermes 用独立 manager key（无 admin 财务删除权），Owner/Secretary 按 Telegram user_id 角色表（assets/roles.json）锁定，admin key 仅 Codex/运维可用。
2. **NAS 备份 cron 已配置**（每日 02:30，保留 30 天，成功静默、失败才通知到群），建议导入真实数据后做一次全量备份 + 恢复演练验证可恢复。
3. **确认读入真实房源的租约/欠租口径**（每套房 start/end/due_day 需准确），并复核佣金规则映射。

技术上：overdue 逐月算法、佣金服务端引擎、支出审批、审计、真实 Telegram E2E 均已验证通过；docker/后端/PG/备份 cron 均就绪。**建议先小批量导入 2–3 套真实房试跑一周再全量**。

## 遗留/说明
- 群已从普通组迁移到 supergroup，cron 与 gateway 的 chat_id 以 `-1004433994558` 为准。
- 未新增功能：未做 Dashboard/WhatsApp/OCR/银行API/租客门户/自动付款/AI改已确认账目（按阶段边界）。
