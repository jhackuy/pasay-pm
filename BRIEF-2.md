# PASay-PM 第二阶段后端增补规格 (BRIEF-2)

> 在阶段一完成的基础上做**最小必要扩展**。只加第二阶段业务真正需要的，禁止扩展成 ERP。
> 项目根: /Users/jhackuy/Documents/Codex/pasay-pm 。现有 31 个端点(CRUD) 不动，只做增量。

## 一、Schema 最小扩展（一个新增 Alembic migration）

按第二阶段需求，对现有表做**局部扩列**（不改已有列名/类型，避免破坏）：

1. **tasks** 表加列（支持周期性维护 / 待办流）：
   - `recurring` BOOLEAN default false
   - `interval_months` INTEGER nullable（recurring 时每 N 个月）
   - `assigned_to` BIGINT nullable（user FK）
   - `completed_at` TIMESTAMPTZ nullable
   - `status` 枚举扩展: 现有 open/in_progress/completed → 增加 `scheduled`、`overdue`(可以用 overdue 计算，不必存)。**只加 `scheduled` 到枚举**；overdue 用 due_date 派生。原有 open 保留。
   - `last_completed_at` nullable（供 recurring 算下一次）
   - `next_due_date` Date nullable（recurring 生成下次到期）

2. **leases** 表加列：
   - `due_day` INTEGER nullable（每月几号交租，如 5）

3. **expenses** 表加列：
   - `due_date` Date nullable（账单到期日，如物业费 Aug 18）
   - `unit_id` BIGINT nullable（若支出关联房源，如物业费；现有没有 unit 关联）

> 说明：income 按阶段一已足够（lease_id+amount+status+received_date）。不新增列。

## 二、高层统计/报告 API（新增 endpoints，非 CRUD）

新增一个 `app/api/routers/reports.py`，前缀 `/api/v1/reports`，全部需鉴权(admin/manager)，返回仅后端计算好的聚合值（**禁止让 Hermes 拉全量自己算**）。

- `GET /api/v1/reports/financial-summary?month=YYYY-MM&unit_id=` →
  ```
  {
    "month": "2026-08",
    "expected_rent_total": "550000.00",   # 本月应收租金(活跃租约 monthly_rent 求和)
    "collected_rent": "430000.00",        # 本月已收(confirmed 租金)
    "outstanding_rent": "120000.00",      # expected - collected
    "total_income": "...",                # 本月所有 confirmed income 合计
    "total_expense": "...",               # 本月已 approved+paid expense 合计
    "net_income": "...",                  # 含佣金前净收入(可选)
    "units_count": 10,
    "occupied_units": 7,
    "vacant_units": 3
  }
  ```
- `GET /api/v1/reports/overdue-rents` → 逾期租金清单: 每个欠租 lease → `{unit, tenant, outstanding, days_overdue}`
- `GET /api/v1/reports/monthly?month=YYYY-MM` → 按单元/租约分组的本月应收/已收/欠租
- `GET /api/v1/reports/commission?month=YYYY-MM&agent_id=` → 中介佣金汇总: `{agent, rule, computed_total, settlements}`
- `GET /api/v1/reports/tasks?status=pending|scheduled|completed&overdue=true` → 待办/逾期任务清单（供"未来30天待办"查询）
- `GET /api/v1/reports/expenses?category=&unit_id=&month=` → 支出汇总/按房源，供"哪个房子维修费最高"

原则：
- 所有聚合在 **SQL/后端** 完成，返回即用 JSON。
- 金额仍全 `Numeric`/`Decimal`，不做 float。
- 只加这些业务需要的。不加通用 BI / 任意维度钻取。

## 三、tasks recurring 相关行为（最小逻辑，不加工作流引擎）

- 创建/更新 task 若 `recurring=true` 且 `interval_months`，创建时算 `next_due_date`。
- 提供 `POST /api/v1/tasks/{id}/complete`：完成时置 completed_at、last_completed_at，并若 recurring 自动派生出**下一条** task（due_date = last_due + interval_months），关联同 unit。返回新旧两条。**这由服务端完成，不是 Hermes 手算**。
- 简单实现即可，不做复杂调度。

## 四、测试

- 为新增 reports endpoints + recurring completion 各加 pytest 用例（复用现有 conftest/test db）。
- 运行 `docker compose run --rm --no-deps -e PYTHONPATH=/app api pytest -q` 全绿。

## 五、验收 / 约束

- `alembic upgrade head` 成功，新增 migration 干净，`alembic check` 无漂移。
- 现有 31 端点不破坏；新增端点可 curl 调用。
- **禁止**：新框架、Redis、消息队列、缓存、权限系统重构、租约表反复造、过度范式化。
- 不写超过需求的 report 变体。

完成后按真实 curl 调用验收后再进入下一步。
