# PASAY-PM — 当前生产架构（2026-08-20，Post PASAY-TASK-011 Closeout）

> **ARCHITECTURE_FROZEN = YES**
> 本文件记录经 Issue #31 (PASAY-TASK-011) 收口后的**唯一目标生产 runtime**。
> 任何拓扑变动必须通过 Issue 重新解冻；禁止私自新增第二套 DB / Bot / Queue / Container 运行栈。

---

## 1. 冻结目标拓扑（唯一生产主链）

```
Telegram api.telegram.org
      │
      │  HTTPS POST /telegram/webhook
      │  + X-Telegram-Bot-Api-Secret-Token
      ▼
┌─────────────────────────────────────────────────────────┐
│  Cloudflare Worker (pasay-cloudflare-worker)             │
│                                                          │
│  (A) fetch handler — Telegram ingress                    │
│        • 校验 method / content-type / secret            │
│        • 解析 update_id + chat_id 仅用于 observability   │
│        • 封装 PASAY-QUEUE-ENVELOPE-V1                    │
│        • PASAY_QUEUE.send(telegram_update)               │
│                                                          │
│  (F) scheduled() handler — Cron ingress                  │
│        • 生成 scheduled_job envelope + deterministic     │
│          5-minute bucket event_id                        │
│        • 同一 PASAY_QUEUE.send(scheduled_job)            │
│                                                          │
│  (C) queue() consumer → Cloudflare Container binding    │
│        • 每条消息 → 唯一 Container                       │
│        • POST /internal/ingest + X-Pasay-Ingest-Token    │
│        • Container 2xx → ack; 4xx malformed → terminal; │
│          5xx / 401 → retry (Queue 自带重试/DLQ)          │
└──────────────────────┬──────────────────────────────────┘
                       │  Cloudflare Queue (pasay-events)
                       │  • 单一队列，Telegram + Cron 共用
                       │  • max_retries=5，死信 → pasay-events-dlq
                       ▼
┌─────────────────────────────────────────────────────────┐
│  Cloudflare Container (pasay-container)                  │
│                                                          │
│  Dockerfile: python:3.11-slim → uvicorn app.main:app     │
│  • PASAY_RUNTIME_MODE=cloudflare-container (hard-coded)  │
│  • entrypoint: alembic upgrade head (DATABASE_URL_UNPOOLED)
│  • 单 FastAPI app / 单 DB boundary / 单 PTB instance     │
│                                                          │
│  ┌─ POST /internal/ingest (Worker 私有 binding 专用) ──┐ │
│  │ • Token: X-Pasay-Ingest-Token (fail-closed)          │ │
│  │ • telegram_update → 复用 app.services.telegram_webhook│ │
│  │   process_telegram_update_payload(db, raw_payload)   │ │
│  │ • scheduled_job → pasay_scheduled_job_ledger 幂等写   │ │
│  │   (INSERT ON CONFLICT DO NOTHING → 200/208)          │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─ POST /telegram/webhook (public, 兼容直接交付) ─────┐ │
│  │ • Worker 不可用时，Telegram 仍可直接投递此端点         │ │
│  │ • 相同 service 层，相同幂等语义                       │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─ GET /health ───────────────────────────────────────┐ │
│  │ • DB liveness + webhook 统计 + architecture 快照      │ │
│  │ • architecture.frozen_topology = worker→queue→…      │ │
│  └──────────────────────────────────────────────────────┘ │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
                Neon PostgreSQL 16
                 (单一业务事实源)
  • telegram_webhook_updates  — webhook 幂等
  • pasay_scheduled_job_ledger — scheduled 幂等
  • 其余 12+ 业务表 (membership / property / rent / …)
  • alembic single-head (未破坏迁移链)
```

---

## 2. 队列消息合同 (PASAY-QUEUE-ENVELOPE-V1)

> **唯一版本**：`version = "1"`
> **禁止**：Avro / Protobuf / Schema Registry / Redis / Kafka / 第二套队列。

| 字段 | 类型 | 说明 |
|---|---|---|
| `version` | `"1"` | 字面量；Container 端严格校验。 |
| `kind` | `"telegram_update"` \| `"scheduled_job"` | 判别器；未知 kind → Queue 端 ack 丢弃。 |
| `event_id` | string | 稳定幂等键：<br>`tg:<update_id>` 十进制字符串<br>`sched:<job_name>:<YYYY-MM-DDTHH-MM>` 5-min 桶 |
| `occurred_at` | ISO-8601 UTC | 事件发生时间。 |
| `payload` | object | `telegram_update` = 原始 Telegram Update JSON（Worker 零解释）<br>`scheduled_job` = `{job_name, scheduled_at, params?}` |
| `_telegram_meta` | `{update_id, chat_id?}` | Telegram 专用 observability 元数据，非业务字段。 |

TypeScript 源：[envelope.ts](file:///d:/AI-Review/pasay-pm/cloudflare-worker/src/envelope.ts)
Python Pydantic 镜像：[envelope.py](file:///d:/AI-Review/pasay-pm/app/schemas/envelope.py)

---

## 3. 历史拓扑（明确标记 HISTORICAL，不作为生产目标）

以下拓扑**不再**是 Pasay 目标生产 runtime：

1. **Long Polling / Hermes 双 consumer 拓扑**（2026-08-10 审计快照）
   - 两个 Hermes gateway + ai-controller 长轮询，共用同一 token
   - 409 conflict 根因；Hermes 仅保留作为独立 supervisor/AI capability，
     **不得再消费同一 bot token 的 updates**。
   - 相关代码：`~/.hermes/…`（部署配置，不在本仓库生产链路引用）

2. **Native Windows PTB polling gateway**（`bin/pasay_runtime.py` / `start-native-bot.ps1`）
   - 标记为 **`development-only`**
   - Dockerfile / Cloudflare Container startup **永不引用**这些脚本
   - 仅供本地 dev/debug，Operator 手动运行，不参与生产链路

3. **Webhook 与 polling 同时作为生产入口的"双主架构"**
   - Issue #31 后已取消：生产只保留 Worker→Queue→Container 单链
   - `/telegram/webhook` 公共端点保留为回退/兼容，但不是首选生产路径。

---

## 4. Cloudflare Worker ↔ Container 投递确认合同

Container `POST /internal/ingest` 返回代码 ↔ Worker Queue consumer 动作：

| Container HTTP | Worker Queue 动作 | 语义 |
|---|---|---|
| **200 / 202 / 208** | `ack()` | 已接受 / 已异步 / 已幂等重放 → 消息移除 |
| **400 / 415 / 422** | `ack()` | 永久 malformed → 丢弃（靠 DLQ 侧人工审计） |
| **401 / 403** | 不 ack，让 Queue retry | Ingest token 未配置 / 错误 → Operator 需介入 |
| **500 / 502 / 503 / 504** | 不 ack，让 Queue retry | Container 瞬时异常 → Queue 重试最多 5 次 → DLQ |
| fetch() 抛异常 | 不 ack，让 Queue retry | Container binding 连接失败 → 同上 |

---

## 5. Long Polling / Legacy Runtime Exit Gate 代码证据

**CONFIRMED BY CODE (T7):**

| # | 证据 | 位置 |
|---|---|---|
| 1 | `Dockerfile CMD` 仅 `uvicorn app.main:app`，纯 HTTP Server | [Dockerfile](file:///d:/AI-Review/pasay-pm/Dockerfile#L61-L68) |
| 2 | `app/main.py` 不 import `bin/pasay_runtime` / `run_operations_worker` / `pasay_bot.main.run_polling` | [main.py](file:///d:/AI-Review/pasay-pm/app/main.py#L79-L100) |
| 3 | `app.services.telegram_webhook.get_ptb_application()` 只调 `build_application()` + `initialize()` + `start()`，只暴露 `process_update()`，**不调 `run_polling()`** | [telegram_webhook.py](file:///d:/AI-Review/pasay-pm/app/services/telegram_webhook.py#L143-L328) |
| 4 | `PASAY_RUNTIME_MODE=cloudflare-container` 在 Dockerfile 硬编码，无法被环境静默降级 | [Dockerfile](file:///d:/AI-Review/pasay-pm/Dockerfile#L18-L22) |

---

## 6. 健康检查边界 (Scope G)

`GET /health` 的 `architecture` 快照字段：

```json
{
  "frozen_topology": "worker→queue→container→neon",
  "runtime_mode": "cloudflare-container",
  "production_runtime_mode_expected": "cloudflare-container",
  "container_ingest_configured": true,
  "db_boundary": {
    "pooled_runtime_url_configured": true,
    "direct_unpooled_migration_url_configured": true
  },
  "long_polling_exit_gate": {
    "import_chain_no_polling_ref": true,
    "production_polling_expected": false
  },
  "telegram_cron_shared_queue": true,
  "architecture_frozen": true
}
```

Worker 侧 `GET /health` 还返回：
- `bindings.queue` / `bindings.container`（binding 是否已配置）
- `secrets_configured.telegram_secret_configured` / `container_ingest_token_configured`

---

## 7. 未实现 / 明确不做事项（严格遵守 Issue #31 Scope）

- ❌ **未实现**：Telegram 3×2 一级菜单 / UX 变更
- ❌ **未实现**：Owner / Secretary 产品逻辑变更
- ❌ **未实现**：Property / Rent / Expense / Repair / Operation 新功能
- ❌ **未实现**：Mini App
- ❌ **未实现**：OpenDesign 变更
- ❌ **未引入**：Redis / Kafka / RabbitMQ / Celery / Temporal
- ❌ **未实现**：Worker 直接写 Neon / Worker 调 PTB / Worker 调 LLM
- ❌ **未重写**：FastAPI 业务层（仅新增 `/internal/ingest` 路由 + 增强 `/health`）
- ❌ **未部署**：不执行 production deploy，不配置 Cloudflare/Telegram/GitHub secrets
- ❌ **未执行**：`setWebhook / deleteWebhook` Telegram API 调用（仅收口仓库代码）
- ❌ **未新增业务 alembic migration**（`pasay_scheduled_job_ledger` 使用 CREATE TABLE IF NOT EXISTS 懒创建，保持 alembic single-head 不变）

---

**Final Architecture Status: `ARCHITECTURE_FROZEN = YES`**
