# PASay-PM — 当前架构审计报告（Phase 1）

> 只读审计产出（2026-08-10，Asia/Manila）。基线：git `main` @ `746b605`。
> 本阶段未修改任何代码/配置；本文件与 `NATIVE_BOT_DESIGN.md` 为唯一新增文件，未提交 git。
> 除文档外 git 树无改动（`git status` 复验：仅 3 个 untracked 项：两份审计简报 + `bin/`，与审计前一致）。

---

## 1. 结论速览

- **仓库里没有任何原生 Telegram Bot 代码**（无 aiogram / python-telegram-bot / InlineKeyboard / callback / sendMessage 业务实现）。当前「Telegram Bot」= Hermes Agent gateway（LLM）通过 `property-management` skill 的 `property_client.py` 以 HTTP Bearer 调 Pasay API，再把 LLM 现生成的文字 `sendMessage` 回显。确认卡片、ASCII 表格均为 **LLM 生成文本**，非确定性 renderer。
- Telegram 消息实际由 **2 个 Hermes gateway 进程**（主 + 受限 profile）+ **1 个无关的 ai-controller 长轮询进程** 消费；其中 ai-controller 与主 Hermes **共用同一生产 bot token**（前缀 `882050…`，@zhushoumacbot），是 409 getUpdates conflict 的直接根因之一。
- 后端（FastAPI + PostgreSQL 16）健康、独立部署于 `/opt/pasay-pm`（native uvicorn，launchd 托管），共 60 个 API 端点、12 张业务表、102 个 pytest 用例。
- RBAC 为两层：后端 `users.api_key_hash` → role（admin/manager/agent）；Telegram 身份 → 角色 仅存在于 Hermes skill 的 `assets/roles.json`（LLM 软约束，**不落库**）。
- 财务写操作全部经 API，业务代码无直接 DB 绕过；但 **income 无唯一约束 / 无 idempotency key**，防重复只靠 LLM 侧「本月已录过」检查。

---

## 2. 系统全景（进程拓扑）

```
Telegram (api.telegram.org, 全部 long polling, 无 webhook)
   │
   ├─ @zhushoumacbot  token 882050…（主 DM + 群 -5417146216 "PASay-PM"）
   │    ├─ Hermes 主 gateway  (launchd ai.hermes.gateway)
   │    │     ~/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run --replace
   │    │     → plugins/platforms/telegram/adapter.py (PTB 22.6, raw do_api_request)
   │    │     → property-management skill (LLM) → property_client.py → Pasay API
   │    └─ ai-controller  (launchd com.ai-controller.bot)   ← 同 token 第二 consumer（409 根因）
   │          /Users/jhackuy/ai-controller/bot.py  (raw urllib, Telegram→Codex CLI bridge)
   │
   └─ @pasayhousebot  token 877703…（受限群 -1004433994558 "pasay houses manage"）
        └─ Hermes profile gateway  (launchd ai.hermes.property-gateway)
              hermes_cli.main --profile=pasay-property gateway run

Pasay API:  /opt/pasay-pm/bin/start-native-api.sh（launchd ai.pasay.api）
            wait pg_isready → alembic upgrade head（fail-closed）→ exec uvicorn 127.0.0.1:8000
PostgreSQL: brew postgresql@16（launchd ai.pasay.postgres）127.0.0.1:5432（docker 容器已停）
```

launchd 清单（`/Library/LaunchDaemons/`，均 `RunAtLoad + KeepAlive`）：

| Label | 进程 | 说明 |
|---|---|---|
| `ai.hermes.gateway` | 主 Hermes gateway (PID ~50213) | @zhushoumacbot，DM + 群 -5417146216 |
| `ai.hermes.property-gateway` | profile gateway (PID ~70760) | @pasayhousebot，群 -1004433994558 |
| `ai.pasay.api` | `/opt/pasay-pm/bin/start-native-api.sh` | uvicorn 127.0.0.1:8000（当前监听） |
| `ai.pasay.postgres` | brew postgres 16 | 127.0.0.1:5432（当前监听） |
| `com.ai-controller.bot` | `/Users/jhackuy/ai-controller/bot.py` | **与主 Hermes 同 token 882050…，需在切流时停用** |

日志：`~/Library/Logs/AI-Agent/*.out.log / *.err.log`；备份：`scripts/backup.sh` + Hermes cron `pasay_nas_backup.sh`（每日 02:30，no_agent）→ `backups/` + NAS `root@192.168.50.27:/volume1/backup/pasay-pm/`。

---

## 3. 后端架构与文件清单

```
~/Documents/Codex/pasay-pm（/opt/pasay-pm 为同一代码的部署副本）
├── app/
│   ├── main.py                 FastAPI 入口，12 个 router 挂到 /api/v1，/health
│   ├── config.py               pydantic-settings（.env：DATABASE_URL / UPLOAD_DIR）
│   ├── database.py             engine / SessionLocal / get_db
│   ├── core/security.py        hash_api_key = SHA-256 hexdigest（明文不落库）
│   ├── api/deps.py             HTTPBearer → users.api_key_hash → require_roles() 守卫
│   ├── api/routers/            auth, properties, units, tenants, leases, income,
│   │                           expense, commission, tasks, reports, attachments, audit
│   ├── models/                 12 张业务表（base.py: AuditMixin/SoftDeleteMixin/pg_enum）
│   ├── schemas/                Pydantic v2（money_field: Numeric(14,2) 校验）
│   └── services/               audit.py（record_audit）、commission_engine.py、
│                               dates.py（add_months/month_range）
├── alembic/versions/           0f9a2e554ec6（初始 12 表）、d7e5c461d569（phase2 列）、
│                               2b4cbce5195f（leases.accounting_start_date）
├── scripts/                    backup.sh, nas_backup_cron.sh, create_api_key.py,
│                               dev_seed.py（纯 API）, dev_cleanup.py（DB 直删 DEV 数据）,
│                               reconcile_check.py（DB 直读对账）, verify_client.py（HTTP 助手）
├── tests/                      9 个文件、102 用例（独立 pasay_pm_test 库，逐用例重建表）
├── bin/start-native-api.sh     开发树 shim → /opt/pasay-pm/bin/start-native-api.sh
├── docker-compose.yml          db(postgres:16-alpine) + api；容器当前全部停止（生产用 native）
└── uploads/ backups/           receipts 与 pg_dump 归档
```

### API 端点清单（60 个，前缀 `/api/v1`）

| Router | 端点 | 权限 |
|---|---|---|
| auth | `POST /auth` | 任何有效 key |
| properties / units / tenants / leases | 各 CRUD 5 个（软删除） | GET 全员，写 manager/admin |
| incomes | GET/POST、`/{id}`、PATCH、`/{id}/confirm`、`/{id}/reverse` | confirm manager/admin；reverse **admin only** |
| expenses | GET/POST、`/{id}`、PATCH、approve/reject/pay/reverse | approve manager/admin（manager 禁自批）；reject/pay/reverse **admin only** |
| commission | rules CRUD（写 admin）、settlements GET/POST、`/{id}/confirm` | confirm **admin only** |
| tasks | GET/POST、`/{id}`、PATCH、DELETE、`/{id}/complete` | POST 全员；PATCH/DELETE/complete manager/admin |
| reports | financial-summary / overdue-rents / monthly / commission / tasks / expenses | manager/admin |
| attachments | POST（multipart）、GET、`/{id}/download` | 全员 |
| audit-logs | GET（table_name/record_id 过滤，limit≤500） | **admin only** |

---

## 4. 必答 17 问

### 1. Telegram Bot 入口在哪里？
仓库内无 Bot 入口。实际入口是 Hermes gateway 进程（见第 2 节拓扑）：
- 主：`~/.hermes/hermes-agent` → `hermes_cli.main gateway run`，Telegram adapter 在 `~/.hermes/hermes-agent/plugins/platforms/telegram/adapter.py`。
- 受限：同代码 `--profile=pasay-property gateway run`。
- 另有第三进程 `com.ai-controller.bot` → `/Users/jhackuy/ai-controller/bot.py`（与主 Hermes 同 token，非 Pasay 业务 bot，但会造成 409）。

### 2. 用哪个 Telegram library？
- Hermes：**python-telegram-bot 22.6**（`~/.hermes/hermes-agent/venv`）。实际发送走 PTB 底层 `bot.do_api_request("sendMessage"/"editMessageText"/"sendRichMessageDraft"/...)` 的**原始 Bot API**，而非高层 `message.reply_text`。
- ai-controller：无库，raw `urllib`/`curl` 直呼 `https://api.telegram.org/bot{token}/{method}`。

### 3. sendMessage / edit_message 在哪里？
- `plugins/platforms/telegram/adapter.py`：`do_api_request("sendMessage", …)`（约 L1883）、`"editMessageText"`（约 L1988）、`"sendRichMessageDraft"`（约 L2071）。
- `ai-controller/bot.py`：`send_text()` / `send_buttons()` → `api_call(token, "sendMessage", …)`（含 `reply_markup.inline_keyboard`），另有 `answerCallbackQuery` / `editMessageText`。

### 4. 当前 parse_mode？
- Hermes：**MarkdownV2 为 legacy 默认**；优先尝试 Bot API 10.1 **Rich Messages**（`sendRichMessage` 带 `markdown` 字段，Telegram 客户端原生渲染），客户端不支持时回退 MarkdownV2。**HTML 未使用**。
- ai-controller：无 parse_mode（纯文本 + emoji）。

### 5. ASCII 表格谁生成？
**LLM 现生成文字**。Hermes adapter 有 `convert_table_to_bullets`（`_wrap_markdown_tables`）把 LLM 输出的 Markdown 表格转成项目符号（Telegram MarkdownV2 无表格语法）。后端只返回 JSON 聚合值，无任何格式化器。

### 6. 是否已有统一 renderer？
**没有**。仅有 Hermes 的文本转义/表格转 bullets 辅助函数，非业务 renderer；后端无 renderer 层。这正是 Native Bot 要补的确定性 renderer。

### 7. 是否已用 InlineKeyboardMarkup / callback_query？
Pasay 业务流：**没有**（SKILL.md 的 `[Confirm]/[Cancel]` 是文字卡片，靠 LLM 语义）。ai-controller 另有一套 InlineKeyboard + callback_query（`approve:<task_id>` / `reject:<task_id>` / 菜单按钮），与 Pasay 无关。

### 8. Telegram callback state 如何保存？
当前无 callback 状态机。ai-controller 用 `data/` 下 JSON/SQLite（chat_id → Codex session/task）持久化；Hermes 用 `~/.hermes/state.db`（SQLite）存会话。Pasay 业务无 callback 状态。

### 9. 房源/财务/租金/逾期 对应哪些 API？
| 业务 | API |
|---|---|
| 房源/单元/租客 | `GET /properties`、`GET /units`、`GET /tenants`、`GET /leases`（组合出「单元 + 活跃租约」） |
| 财务汇总 | `GET /reports/financial-summary?month=&unit_id=`、`GET /reports/monthly`、`GET /reports/expenses` |
| 逾期租金 | `GET /reports/overdue-rents`（逐月明细 + total_outstanding + overdue_days） |
| 收租 | `POST /incomes {status:pending}` → `POST /incomes/{id}/confirm` → `POST /incomes/{id}/reverse`(admin) |
| 支撑 | `GET /reports/commission`、`GET /reports/tasks`、`GET /audit-logs`(admin)、`POST /auth` |

### 10. 收租 confirm/reverse 当前链路
1. Hermes LLM 解析「Unit 1203 paid 65000 August」→ 校验（unit 存在、金额≈月租、本月未录过、租约 active）——校验在 **LLM 侧**，后端不 enforce。
2. `property_client.py record_rent_pending` → `POST /api/v1/incomes {status:"pending", lease_id, amount, received_date, payment_method, description:"…2026-08…"}`（manager/admin）。
3. LLM 回文字确认卡 `Rent Entry … [Confirm] [Cancel]`。
4. 用户确认 → `confirm_rent` → `POST /incomes/{id}/confirm`（manager/admin）：后端校验 `status==pending`，置 confirmed + confirmed_by/at，写 audit（action=confirm）。
5. `reverse_income` → `POST /incomes/{id}/reverse`（**admin only**）：校验 `status==confirmed` → reversed + audit。
6. 金额全链路 Decimal 字符串，后端 `Numeric(14,2)` + Pydantic `money_field`。

### 11. API Key / RBAC 如何实现
- 后端：`hash_api_key()` = SHA-256；`deps.get_current_user` 按 `users.api_key_hash == hash(bearer)` + `is_active` 认证；`require_roles(admin/manager/agent)` 403 守卫。`users` 表**没有 telegram_user_id 列**。
- Telegram 层：Hermes skill `assets/roles.json`：`5177241442→OWNER`（query_all/confirm_income/approve_expense/reverse_finance/view_stats/sensitive_ops）、`1083657401→SECRETARY`（record_income/submit_expense/upload_receipt/log_maintenance/complete_todo）。判定靠 LLM 按 SKILL.md 规则执行，属**软约束**；skill 同时用单一 `PROPERTY_API_KEY`（当前为 admin 级，开发期，PHASE2 报告建议正式导入前降为 manager 级）。

### 12. 审计日志如何实现
- `services/audit.py::record_audit(db, table_name, record_id, action, actor_id, changed_fields, old_value, new_value)` → `audit_logs`（JSONB 快照），由每个 router 写操作**手动调用**并随业务一起 commit；无 SQLAlchemy event 自动审计。
- 查询：`GET /audit-logs`（admin only，table_name/record_id 过滤、limit≤500）。action 枚举：create/update/soft_delete/confirm/approve/reject/pay/reverse。

### 13. 重复写入风险
- **存在**。`incomes` 只有 `ix_incomes_lease_id`（非唯一），**无 (lease_id, 期间) 唯一约束、无 idempotency key**；「本月已录过」仅 LLM 行为约定，后端不 enforce → 重试/并发可产生重复 pending/confirmed income。
- `confirm`/`reverse` 有状态守卫（非 pending/非 confirmed → 409），但无乐观锁/版本号：两个并发 confirm 可能都通过读-改-写，存在 last-write-wins 竞态（实际影响小：只写 status + confirmed_at）。
- `tasks/{id}/complete` 有 409 防重；`audit_logs` 允许重复行（可接受）。

### 14. 是否存在直接 DB 写入绕过 API？
- 业务代码（`app/`）：**无**。
- 运维/DEV 脚本（有意为之，非业务路径）：`scripts/create_api_key.py`（直写 users，引导创建 key）、`scripts/dev_cleanup.py`（直删 DEV 标记数据，因财务无 DELETE 端点，注释已说明）；`scripts/reconcile_check.py` 仅直读对账（psycopg2，无写）。
- Hermes skill 明文规定：**绝不直接碰 PostgreSQL，不用 Memory 当账本**。

### 15. expense 审批流 / reverse 权限边界
- create：manager/admin；`status` 仅允许 pending/approved；**非 admin 禁止直接创建 approved**。
- approve：manager/admin；**manager 不能批自己创建的**（admin 可批自己的，commit `eec614f` 修复）。
- reject：**admin only**，且不能拒自己创建的。
- pay：**admin only**（approved→paid）。
- reverse：**admin only**（paid→reversed）。
- income：confirm manager/admin；reverse **admin only**。commission settlement confirm：**admin only**。财务记录均无 DELETE。

### 16. 测试覆盖与缺失
- **102 个用例**（`pytest --collect-only` 实测；简报写的 103 有出入）：auth 6、properties 10、tenants 4、leases 14、financial 18、commission 10、reports 27、tasks 8、audit 5。
- conftest：独立 `pasay_pm_test` 库 + 逐用例 `drop_all/create_all` + TestClient `dependency_overrides`。
- 缺失：Telegram/Bot 层测试（该层尚不存在）；`property_client.py` 无测试（在 Hermes skill 内）；无 idempotency/并发/双 confirm 竞态测试；无 Hermes↔Bot adapter 集成测试；E2E 仅 PHASE2 用 computer_use 人工驱动真实群。

### 17. 准备实现 Native Bot 时改/新增的文件清单
见 `NATIVE_BOT_DESIGN.md` §3（目录结构）与 §11（后端配合改动）。原则：**新增 `pasay-telegram-bot/` 独立 service 目录，不改动现有后端业务代码**；后端仅后续阶段做可选小改（idempotency/telegram_user_id）。

---

## 5. 补充审计 A–H

### A. 真实部署位置
- 生产 API = `/opt/pasay-pm`（`/opt` 下的非保护路径副本，避免 launchd 对 `~/Documents` 的 GUI/FDD 依赖）；`bin/start-native-api.sh` 由 `ai.pasay.api.plist` 拉起：等 PG → `alembic upgrade head`（**fail-closed**，迁移失败不启 uvicorn）→ `exec uvicorn --host 127.0.0.1 --port 8000`。
- PG = brew `postgresql@16`，`ai.pasay.postgres.plist` → `/opt/homebrew/opt/postgresql@16/bin/postgres -D /opt/homebrew/var/postgresql@16`。
- Hermes 两 gateway 见第 2 节；`com.ai-controller.bot.plist` 是第三守护进程。
- 开发树 `bin/start-native-api.sh` 是 shim，转发到 `/opt/pasay-pm/bin/start-native-api.sh`（单一入口）。

### B. Docker / repo 结构
- `docker-compose.yml`：`db`（postgres:16-alpine, host 5432）+ `api`（build, :8000, entrypoint 先 `alembic upgrade head`）。**当前容器全部停止**（`docker ps` 为空），生产走 native。
- 12 张业务表 + `alembic_version`；3 个迁移（initial / phase2_columns / accounting_start_date）。
- 注意：`README.md` 写「11 个 router」，实际 `main.py` 挂载 **12 个 router**（文档小瑕疵）。

### C. 生产 pasay bot token 注册点
| Token | 注册处 | Bot | 服务对象 |
|---|---|---|---|
| `882050…`（len 46） | `~/.hermes/.env`（TELEGRAM_BOT_TOKEN）**且** `/Users/jhackuy/ai-controller/.env` | @zhushoumacbot | 主 DM + 群 -5417146216 |
| `877703…`（len 46） | `~/.hermes/profiles/pasay-property/.env` | @pasayhousebot | 受限群 -1004433994558 |

→ 主 token 有**两个 consumer**（Hermes + ai-controller），即 409 根因；profile token 目前单 consumer。

### D. polling 还是 webhook
**全部 long polling**：两个 Hermes gateway 均 PTB `start_polling()`（`TELEGRAM_WEBHOOK_URL` 被注释）；ai-controller 用 `getUpdates` 长轮询（curl 硬超时 25s）。无 webhook 部署。

### E. Hermes 是否已有本地 HTTP/session API
- 代码**具备**但**未启用**：`gateway/platforms/api_server.py`（OpenAI 兼容，`http://127.0.0.1:8642/v1`，`API_SERVER_KEY` 鉴权；含 `POST /api/sessions`、`POST /api/sessions/{id}/chat[/stream]`、`/v1/chat/completions`、`/v1/runs`+SSE；multiplex 时用 `/p/<profile>/` 前缀）。`gateway/platforms/webhook.py`（HMAC + routes + deliver）同样存在未启用。
- 实测：`127.0.0.1:8642` **无监听**；主/受限 config.yaml 均无 `api_server`/`webhook` 段。当前可用的本地入口只有 CLI（`codex exec` / Hermes 自己的 `serve` dashboard，:63248，非 API server）。

### F. 最薄 Hermes adapter 设计
- 入站（Native Bot 收到自然语言 → Hermes 推理）：
  1. **推荐**：启用 `api_server` platform（config.yaml `gateway.platforms.api_server` + `API_SERVER_KEY`），Native Bot `POST /api/sessions/{sid}/chat`（或 `/v1/chat/completions` + `X-Hermes-Session-Key`），Hermes 返回结构化 JSON 文本/工具结果，Native Bot 提取后交给确定性 renderer。改动 = 1 段 config + 1 个本地 HTTP client，无 Hermes 代码改动。
  2. 备选：Native Bot 直接 spawn `codex exec`（同 Hermes 底层的 CLI）——零 Hermes 配置，但绕过 gateway、重复会话管理，**不推荐**。
- 出站（Hermes 主动消息，如每日 08:00 摘要 cron）：
  1. 启 `webhook` platform 或让 cron job 的 `deliver` 指向 Native Bot 的本地 HTTP hook（`127.0.0.1:8001/hook/hermes`，HMAC 签名），Native Bot 渲染后 `sendMessage`。
  2. 或：把每日摘要 cron 迁到 Native Bot（确定性 renderer + JobQueue），Hermes 只保留 NL 会话。
- 契约：JSON envelope `{chat_id, user_id, text, structured: {...}?, message_id}`；Hermes 返回 `{text, structured, tool_events}`。

### G. property skill 能否复用
- **能**。`SKILL.md` + `scripts/property_client.py`（17 个业务 action：list_properties / record_rent_pending / confirm_rent / reverse_income / create_expense_request / approve|reject|pay_expense / create|complete_task / get_*reports 等）继续作为 **Hermes 端 adapter**（NL → 业务 action → Pasay API），Native Bot 不做 NL 时不需要引入它们。
- `roles.json`（OWNER/SECRETARY + can 列表）是 Native Bot 角色设计的蓝本，建议迁入后端（users.telegram_user_id）以硬约束替代 LLM 软约束。
- `property_client.py` 的 action 命名可直接镜像为 Native Bot 的按钮/命令 → Pasay API 调用。

### H. Telegram 用户/群 → Hermes session 映射
- 两 gateway 的 allow 配置一致：`TELEGRAM_ALLOWED_USERS=5177241442,1083657401`（= OWNER + SECRETARY）；`TELEGRAM_GROUP_ALLOWED_USERS` 同。
- 群：主 `TELEGRAM_GROUP_ALLOWED_CHATS=-5417146216`（HOME_CHANNEL 同名，channel_directory 名为 "pasay houses manage"）；profile `TELEGRAM_GROUP_ALLOWED_CHATS=-1004433994558`（"pasay houses manage"）。主 gateway 的 channel_directory 还残留旧群 -1004433994558（未在 allowlist，不生效）。
- Hermes session 按 chat 建立（`group_sessions_per_user: true`），存 `state.db` / `sessions/`；角色→权限由 roles.json 决定（§5/Q11）。
- Native Bot 需独立映射：`telegram_user_id → 后端用户/角色`，建议后端 `users.telegram_user_id` 唯一列（迁移），Bot 每次 update 用 `effective_user.id` 解析角色。

---

## 6. 当前问题与风险清单

| # | 问题 | 风险/影响 | 建议 |
|---|---|---|---|
| P1 | 主 token 双 consumer（Hermes + ai-controller） | 409 getUpdates，消息偶发丢失/重复 | 切流第一阶段停 `com.ai-controller.bot` |
| P2 | 确认卡片/表格 = LLM 自由文本 | 格式漂移、金额/日期解析错、不可点击 | Native Bot 确定性 renderer + InlineKeyboard |
| P3 | income 无唯一约束 / idempotency | 双击/重试/并发重复入账 | Bot 层 idempotency + 后端唯一约束（后续） |
| P4 | Telegram 身份→角色不落库（roles.json + LLM 软约束） | SECRETARY 可借 LLM 越权（confirm 等） | users.telegram_user_id + 后端硬约束 |
| P5 | `PROPERTY_API_KEY` 为 admin 级（开发期） | 单 key 全权限，泄 key 即全权 | 拆 manager key 给 Hermes/Bot，admin key 仅运维 |
| P6 | 无统一 renderer / 无 Bot 层测试 | 回归靠人工 | 新增 Bot 测试套件 |
| P7 | README 与实现不符（11 vs 12 router） | 文档误导 | 顺手修正 README（下一提交） |
| P8 | 每日 08:00 摘要 cron 由 LLM 生成 | 内容不可控、格式漂移 | 迁至 Native Bot JobQueue 确定性渲染 |

---

## 7. 推荐重构方案（目标架构）

```
Telegram
   ├─ pasay-telegram-bot ── 确定性 commands/buttons/卡片(HTML+InlineKeyboard) ──→ Pasay PM API
   └─ natural language ──→ Hermes ── intent/tool routing（复用 property skill）──→ Pasay PM API
          ↑ structured result（Hermes api_server / 本地 HTTP adapter）回传 Native Bot renderer 输出 UI
```

- 新增 `pasay-telegram-bot` 独立 service（同仓库新目录，独立 venv/launchd），原生 PTB 22.x，HTML parse_mode + 卡片式 + InlineKeyboard，财务写操作一律走 Pasay API。
- Native Bot 成为生产 token 的**唯一 consumer**（scoped cutover，见 `NATIVE_BOT_DESIGN.md` §9）。
- 本轮范围：房源/财务/逾期/收租四块做全量按钮化；维修/佣金/租约编辑保留入口与现状，不重构。
