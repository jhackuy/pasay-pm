# PASay V1.1 Final Financial Safety Hardening — Codex Max Brief

你是 Codex Max (Principal Engineer + Financial-Safety Reviewer)。生产财务系统冻结前的最后一道安全加固。目标唯一且不可妥协：

> **任何会改变财务或业务状态的操作，无论重复点击 10 次、旧卡片重放、网络超时重试、并发 update 同时抵达，最终 DB 结果必须与只执行 1 次完全一致。**

**最终安全边界必须落在 backend + PostgreSQL 层，禁止只依赖 Telegram/UI 层的防双击。**

---

## 0. 环境（Hermes 已核对，直接使用）

- Git 工作根：`/Users/jhackuy/Documents/Codex/pasay-pm`（唯一可运行的 git 目录，**在此工作**）。分支 `feature/telegram-ui-v2` @ `55b4edf`。
- 生产运行时副本：`/opt/pasay-pm`（Hermes 负责同步/重启，你不用碰 /opt，可只读看日志/查库）。
- 后端：FastAPI + SQLAlchemy + **真实 PostgreSQL**（pg16）。DB URL 在 git 源 `app/config.py` / `/opt/pasay-pm/.env`。
- **测试基础设施已用真实 Postgres**（`tests/conftest.py` 建独立 `pasay_pm_test` 库）。并发 race 测试必须沿用真实 Postgres，禁止 mock 绕过。
- 运行 python/pytest **必须 `env -u PYTHONPATH`**，用 `.venv/bin/python`。
- 当前基线：Bot 132 passed / Backend 102 passed。

## 1. 已审计确认的竞态（Hermes 初步定位，你需复核细节并修复）

所有写路径都是 **SELECT-then-status-check-then-UPDATE** 模式（`_get_or_404` 读 → `if obj.status != X: 409` → setattr → commit）。这是典型 **TOCTOU**：

| 端点 | 位置 | 竞态 |
|---|---|---|
| `POST /incomes` create_income | income.py:37-67 | 无幂等 key，重复调用产生 N 条相同业务收入 |
| `POST /incomes/{id}/confirm` | income.py:116-140 | 并发 confirm：状态判断非原子，可能产生 2 条 confirm audit |
| `POST /incomes/{id}/reverse` | income.py:143-165 | 并发 reverse：同样竞态 |
| `POST /expenses/{id}/approve` | expense.py:132+ | `if status != pending` 非原子 |
| `POST /expenses/{id}/reject` | expense.py:164+ | 同上 |
| `POST /expenses/{id}/pay` | expense.py:194+ | `if status != approved` 非原子 |
| `POST /expenses/{id}/reverse` | expense.py:220+ | `if status != paid` 非原子 |
| `POST /commission/settlements/{id}/confirm` | commission.py:212+ | `if status != pending` 非原子 |

**schema 现状（alembic/versions/0f9a2e554ec6）：`incomes` / `expenses` / `commission_settlements` 均无任何 UNIQUE 约束，无 idempotency_key 列。** 唯一有 UNIQUE 的是 `users.api_key_hash`、`users.username`。即幂等目前完全没有 DB 级兜底。

## 2. 必须实现的修复（按优先级）

### P0 — create 幂等（唯一最强保障）
给 `incomes` 增加 `idempotency_key` 列 + **UNIQUE constraint**（immutable）。create 时：
- Bot/API 若提供 `idempotency_key` → 存在则**返回已有记录**（200/201 幂等），不存在才插入；插入靠 UNIQUE **原子兜底**（冲突 → IntegrityError → 重新查回已有记录返回，绝不 500）。
- 不提供 key 时维持现状（但按当前业务由 bot 层必传）。
- **生产 migration 前先检测**现有 incomes 是否可安全加 UNIQUE（重复 key 检测脚本），不允许因加约束导致生产启动失败。给出检测 SQL + rollback。

### P1 — 状态转换原子化（conditional UPDATE）
把 confirm/reverse/approve/reject/pay/settlement-confirm 全部改为**原子 conditional UPDATE**：
```sql
UPDATE incomes SET status='confirmed', ... WHERE id=:id AND status='pending'
```
用 SQLAlchemy 的 `update().where(...).values(...)` + `result.rowcount`。
- `rowcount == 1` → 成功（并在此事务内写一条 audit）
- `rowcount == 0` → 已是其他状态 → **幂等/冲突**：重查当前状态，若已是我们想转换到的目标状态则返回现状（可重复调用），否则 409。**绝不能重复记账/重复写 audit。**
- **关键**：状态转换 + side-effect 记账（confirm 扣应收、pay 记 ledger 等，If 当前模型有）必须在**同一个 DB transaction** 原子完成。任何 side effect 都不允许落在 conditional UPDATE 之外。

### P2 — settlement 幂等
`commission_settlements`：确认是否有 "确认即产生佣金" 的全局唯—需求（同一 (agent, lease, rule) 只一条活跃 settlement？）。若业务将同一 settlement 重复 confirm 视为不能重复支付，则加对应 UNIQUE / 或 conditional 状态机。**不要擅自改变佣金计算引擎的既有正确语义**，只保证 confirm 幂等。

### P3 — 明确 UserDefaults vs 幂等职责（不要混淆）
- `pasay_bot/state/store.py` 的 `user_defaults`（SQLite）= **纯 UI 偏好**（最近付款方式等），不是幂等保障。
- 幂等 nonce / idempotency key 的**最终裁决必须在 backend**（见 P0/P1）。Bot 层 nonce 只是用户体验层去重，不是安全边界。
- 审计 `pasay_bot/state/idempotency.py` 现有 nonce 机制，确认它如何与 backend `idempotency_key` 对接，确保 **Bot 重启/旧卡片重放后**，若 DB 已落盘则 reconcile 返回已有记录、不二次写入。

### E — 超时/错误语义
确认 Bot 层 timeout 后的 reconcile 路径（已有 `_confirm_rent_entry`、find_income 对账）。加固：任何 create 类操作 timeout 后**禁止裸发第二笔**；必须：
1. 用原 idempotency_key 查询/reconcile
2. 已成功 → 返回已有
3. 明确未执行 → 才 retry（继续用原 key）

## 3. 并发测试（新增 `tests/test_financial_idempotency.py`，真实 Postgres）

系统级 invariant（必须有一条测试显式断言）：
> Repeating the same financial command N times, sequentially or concurrently, produces the same final business state as executing it once.

覆盖（无法 mock DB，跑真实 Postgres 并发）：
- create income：同 key sequential ×10 / 同 key concurrent ×10 / 不同 request 同一业务事件并发 → **incomes 行 =1**，audit create =1
- confirm income：concurrent ×10 → 状态只变一次、audit confirm =1、无重复 side effect
- reverse income：concurrent ×10 → 原 income 最终 reversed、只冲销一次（reversal 只 1）
- expense approve/reject/pay：concurrent ×10 → 状态各只变一次、pay 只记一次
- commission settlement confirm：concurrent ×10 → settlement/payment 只一次
- timeout-after-commit 模拟（DB 已 commit，HTTP response 丢失 → 同 key retry 必须返回已有、不二次创建）

**并发执行**：用 `concurrent.futures.ThreadPoolExecutor` 或独立连接池同时打条件 UPDATE / 同 key create，直接断言 DB 最终状态。同 key 并发 create 必须验证 UNIQUE 兜底只插 1 行。

必须先给出**检测**：加 UNIQUE 前对生产库跑重复检测 SQL（只读），确认无历史冲突再迁移。

## 4. Migration
新增一个 alembic revision（如 `financial_idempotency`）：
- incomes 加 `idempotency_key VARCHAR(128)` nullable + `CREATE UNIQUE INDEX ... ON incomes(idempotency_key) WHERE idempotency_key IS NOT NULL`（partial unique，保留历史 NULL 行）。
- 如需其它约束（reversal source 等）一并评估。
- **production 检测**：跑 `SELECT idempotency_key, count(*) ... GROUP BY HAVING count(*)>1` 确认 0 冲突后才应用。
- 写 `downgrade()`，提供 rollback。
- 在测试库跑 migration 验证；也验证新代码在旧库（无 key 的历史行）上不影响现有数据。

## 5. Git 纪律 / 交付
- 只在 git 源改；**不要 commit 到 main**，工作分支 `feature/telegram-ui-v2`。
- 不要碰 `/opt`、不要启动新 cron、不要加新功能。
- 运行 `tests/` 全量：backend 原 102 不减，新增本文件所有并发测试全绿。
- 完成后产出 `ux/` 同级一个 `FIN_SAFETY_REVIEW.md`：竞态清单→修复→每个操作的保护类型（UNIQUE/conditional/tx）→并发测试结果表（sequential ×10、concurrent ×10、timeout-after-commit、stale callback）→生产检测 SQL 结果→migration 版本→rollback 说明→**剩余已知重复入账/撤销/付款风险清单**。
- 最后 print 纯文本总结，回答 Hermes 报告所需的 14 个点（idempotency 现状、发现的 race、修复、每个操作的保护、各项并发测试结果、bot/backend 测试数、二轮 review 结论、剩余风险）。结尾打 `CODEX_MAX_FIN_DONE`。
