# PASay Property Management — V1 第一阶段工程简报 (BRIEF)

> 本文件是唯一的工程规格。**只做第一阶段描述的内容，绝不扩展。**
> 核心原则：**轻量、稳定、可维护**。业务规模只有十几套出租房。
> 任何"未来可能用到"的设计都必须拒绝。保持简单。

---

## 0. 规模与定位

这是一个小型物业管理后端，管理 ~10 套出租房。系统必须：
- 单一后端服务 (FastAPI) + PostgreSQL
- 运行在 macOS (Docker Desktop) 本机，通过 Docker Compose
- Hermes Agent 通过安全 API（API Key + 角色）调用
- Telegram 是主要操作界面（通过 Hermes，不在此阶段实现 Telegram BOT）
- 数据库是唯一真实数据源，金额精确。

**不要做**：WhatsApp, OCR, 银行API, 租客Portal, 复杂网页UI, 自动付款, AI自动修改已确认账目, 微服务, 消息队列, Redis 缓存, Kubernetes。

---

## 1. 技术栈（固定，不可更替）

- Python 3.11+, FastAPI (最新稳定), Uvicorn
- SQLAlchemy 2.x (ORM) + Alembic (schema migration)
- PostgreSQL 16
- Pydantic v2
- 认证: HTTP Bearer API Key (每个客户端固定 key) + role 字段。**不引入 Auth0/SSO/OAuth2 复杂流程**。
- 密码/密钥: 环境变量 + `.env`（不提交仓库）
- 测试: pytest + httpx (TestClient) + PostgreSQL (测试库)
- 部署: Dockerfile + docker-compose.yml (postgres + api 两个服务)
- 备份: shell 脚本 `pg_dump` → 压缩 → `rsync`/`scp` → NAS (SSH)

---

## 2. 目录结构（固定）

```
pasay-pm/
├── BRIEF.md
├── README.md
├── docker-compose.yml
├── .env.example
├── .gitignore
├── Dockerfile
├── requirements.txt
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/          # migration 脚本
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI 应用入口
│   ├── config.py          # 设置 (pydantic-settings)
│   ├── database.py        # engine/session
│   ├── models/            # SQLAlchemy 模型
│   │   ├── __init__.py
│   │   ├── base.py        # Base + AuditMixin
│   │   ├── user.py        # User/ApiClient
│   │   ├── property.py    # Property, Unit
│   │   ├── tenant.py      # Tenant
│   │   ├── lease.py       # Lease
│   │   ├── financial.py   # Income, Expense
│   │   ├── commission.py  # CommissionRule, CommissionSettlement
│   │   ├── task.py        # Task/Maintenance
│   │   ├── attachment.py  # Attachment
│   │   └── audit_log.py   # AuditLog
│   ├── schemas/           # Pydantic schemas
│   ├── api/
│   │   ├── __init__.py
│   │   ├── deps.py        # auth dependency, role guard
│   │   └── routers/
│   │       ├── __init__.py
│   │       ├── auth.py
│   │       ├── properties.py
│   │       ├── tenants.py
│   │       ├── leases.py
│   │       ├── income.py
│   │       ├── expense.py
│   │       ├── commission.py
│   │       ├── tasks.py
│   │       ├── attachments.py
│   │       └── audit.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── commission_engine.py   # 佣金规则引擎（非LLM）
│   │   └── audit.py               # 审计日志记录
│   └── core/              # 若需要
├── scripts/
│   └── backup.sh          # 自动备份脚本 (NAS)
└── tests/
    ├── conftest.py
    ├── test_auth.py
    ├── test_properties.py
    ├── test_tenants.py
    ├── test_leases.py
    ├── test_financial.py
    ├── test_commission.py
    ├── test_tasks.py
    └── test_audit.py
```

---

## 3. 数据模型（10 张表核心）

所有表: 主键 `id BIGSERIAL`; 继承 `AuditMixin`（`created_at`, `updated_at`, `created_by`, `updated_by`）。
**金额一律 `NUMERIC(14,2)`, 严禁 float。** 时区统一用 `timestamptz`。软删除用 `deleted_at` (只用于非财务数据)。

保留 10 张核心表，**不要额外拆表**：

1. **`users`** — 系统账户/API 客户端。字段: `username`, `role`(enum: admin, manager, agent), `api_key_hash`, `is_active`。用于认证和"操作者"审计。
2. **`properties`** — 房产: `name`, `address`, `city`, `total_units`, `is_active`。
3. **`units`** — 单元: `property_id FK`, `unit_number`, `floor`, `size_sqm`, `monthly_rent NUMERIC`, `status`(vacant/occupied/maintenance), `is_active`。
4. **`tenants`** — 租客: `full_name`, `phone`, `email`, `nationality`, `id_document`, `emergency_contact`, `is_active`。
5. **`leases`** — 租约: `unit_id FK`, `tenant_id FK`, `start_date`, `end_date`, `monthly_rent NUMERIC`, `deposit NUMERIC`, `status`(active/expired/terminated), `notes`。租约与 unit 的占用状态联动（开始占用、终止释放）。
6. **`incomes`** — 收入/租金: `lease_id FK`, `amount NUMERIC`, `received_date`, `payment_method`, `status`(pending/confirmed/reversed), `description`, `confirmed_by`, `confirmed_at`。
7. **`expenses`** — 支出: `expense_date`, `category`, `amount NUMERIC`, `payee`, `description`, `status`(pending/approved/rejected/paid/reversed), `approved_by`, `approved_at`, `receipt_attachment_id FK`。**支出支持审批流**（创建→pending，admin approve→approved，财务支付→paid）。
8. **`commission_rules`** — 佣金规则: `name`, `rule_type`(percentage/flat), `value NUMERIC`, `agent_role`(出租/出售/管理), `is_active`。
9. **`commission_settlements`** — 佣金结算: `agent_id FK(user)`, `lease_id FK`, `rule_id FK`, `computed_amount NUMERIC`, `status`(pending/confirmed), `notes`。**金额必须由 commission_engine 按 rule 计算，禁止硬编码/LLM 算**。
10. **`audit_logs`** — 审计日志: `table_name`, `record_id`, `action`(create/update/soft_delete/confirm/approve/reverse), `actor_id`, `changed_fields`(JSONB: 修改前→后), `old_value`(JSONB), `new_value`(JSONB), `created_at`。

**额外但必要的表**（属基础设施，不算过度设计）:
- **`attachments`** — 附件/收据: `filedata` 存文件路径, `original_filename`, `mime_type`, `related_type`, `related_id`(多态关联), `uploaded_by`, `uploaded_at`。文件存本地 `uploads/` 目录，DB 存路径。

财务禁忌（**必须强制执行**）:
- 已 confirmed 的 income/expense **禁止 DELETE**（API 层抛 409）。只允许 `reversed`（冲销），冲销记录也进 audit_logs。
- 所有财务**写入必须带 status**（pending/confirmed/approved 等），禁止"裸写"。
- expense 审批: 只有 admin 能 approve，approve 后禁止直接修改金额（如需改先 reject/reverse）。

---

## 4. API 清单（第一阶段全部端点）

前缀 `/api/v1`。认证: `Authorization: Bearer <api_key>`。角色守卫: `admin` 全权，`manager` 大部分，`agent` 仅限查看和自己的相关记录。

- `POST /api/v1/auth` — 校验 api key 返回 client info（供 Hermes 探测）
- `GET /health` — 健康检查（不鉴权）
- Properties: `GET/POST /api/v1/properties`, `GET/PATCH/DELETE /api/v1/properties/{id}`
- Units: `GET/POST /api/v1/units`, `GET/PATCH/DELETE /api/v1/units/{id}`
- Tenants: `GET/POST /api/v1/tenants`, `GET/PATCH/DELETE /api/v1/tenants/{id}`
- Leases: `GET/POST /api/v1/leases`, `GET/PATCH/DELETE /api/v1/leases/{id}`
- Income: `GET/POST /api/v1/incomes`; `POST /api/v1/incomes/{id}/confirm`; `POST /api/v1/incomes/{id}/reverse`（无 DELETE 已确认）
- Expense: `GET/POST /api/v1/expenses`; `POST /api/v1/expenses/{id}/approve`; `POST /api/v1/expenses/{id}/reject`; `POST /api/v1/expenses/{id}/pay`; `POST /api/v1/expenses/{id}/reverse`（无 DELETE）
- Commission: `GET/POST /api/v1/commission/rules`, `GET/PATCH/DELETE /api/v1/commission/rules/{id}`; `GET/POST /api/v1/commission/settlements`; `POST /api/v1/commission/settlements/{id}/confirm`
- Tasks: `GET/POST /api/v1/tasks`, `GET/PATCH/DELETE /api/v1/tasks/{id}`
- Attachments: `POST /api/v1/attachments`(multipart), `GET /api/v1/attachments/{id}`, `GET /api/v1/attachments/{id}/download`
- Audit: `GET /api/v1/audit-logs`（admin 可查，支持 table_name/record_id 过滤）

API 错误格式统一: `{"detail": "<message>"}`。关键写操作返回 409（冲突）而非静默失败。

---

## 5. 权限模型

- 简单 RBAC: `role` in {admin, manager, agent}。
- `admin`: 全部权限 + 审批支出 + 管理 users + 校准佣金。
- `manager`: 全部业务 CRUD，但不能审批自己创建的支出（可选，简单实现即可），可确认收入。
- `agent`: 只读大部分 + 创建 task + 查看自己相关的 lease/commission。
- 依赖注入 `get_current_user` 校验 api_key → 解出 role → 在 router 用依赖守卫。
- **不做** 细粒度 per-object ACL / 组织租户模型 / 多租户。单组织即可。

---

## 6. 佣金规则引擎（非 LLM）

`app/services/commission_engine.py` 提供纯函数:
```
compute_settlement(settlement, rule, lease_amount) -> Decimal
```
- `rule_type=percentage`: `computed = lease_amount * value/100`
- `rule_type=flat`: `computed = value`
- 佣金结算创建时**必须调用引擎**计算结果写入 `computed_amount`。任何 API 都不接受客户端直接传 computed_amount。加单测验证。
- 这是"AI 不允许直接写账"的核心防线。

---

## 7. 测试要求（第一阶段跑通）

使用 pytest + SQLAlchemy + httpx。conftest 创建独立 test database（`pasay_pm_test`），每个测试事务回滚或重建表。
覆盖: auth(无效key/角色), 各 CRUD, 财务禁止删除已确认/必须带status, 支出审批流转, 佣金引擎计算, 审计日志记录。**至少 20 个用例，能绿色跑完。**

---

## 8. Docker Compose

```yaml
services:
  db:
    image: postgres:16-alpine
    environment: POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD (从 .env)
    volumes: pgdata:/var/lib/postgresql/data
    ports: "5432:5432"
    healthcheck: pg_isready
  api:
    build: .
    depends_on: db (healthy)
    env_file: .env
    ports: "8000:8000"
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    volumes: ./uploads:/app/uploads
volumes: pgdata:
```
启动后自动跑 `alembic upgrade head`。

---

## 9. 备份到 NAS（本机可实测）

`scripts/backup.sh`:
- `pg_dump` (custom format) → gzip → 时间戳文件名
- 用 `rsync` (走 SSH, 免密 key `~/.ssh/pmp_pasay_backup`) 推送到 NAS
- NAS 目标（已配置免密已验证）: `root@192.168.50.27:/volume1/backup/pasay-pm/`
- 保留最近 N 份（如 30 份）本地+远程清理
- 配置可覆盖: `BACKUP_REMOTE`、`BACKUP_KEEP`、`DATABASE_URL` 用环境变量
- 该脚本必须真实可运行（我会实测一次）

---

## 10. README 与部署/恢复说明

README 覆盖: 架构图(文字), 快速启动 (`docker compose up`), 环境变量说明, 如何生成 API key, 如何跑测试, 备份/恢复步骤(含 NAS), 权限说明。恢复: `pg_restore` 步骤写清楚。

---

## 11. 交付验收清单（第一阶段的"完成"定义）

1. ✅ `docker compose up` 一条命令把 db+api 跑起来
2. ✅ Alembic migration 全量建 11 张表
3. ✅ 所有 API 端点可调用（含鉴权/角色/审计）
4. ✅ pytest 全绿（≥20 用例）
5. ✅ `scripts/backup.sh` 能把 pg dump 推到 NAS 指定目录（实测）
6. ✅ README 完整，含恢复步骤
7. ❌ 不出现任何"未要求但顺手加了"的功能

---

## 实施约束（对执行者）

- 用 SQLAlchemy 2.x 声明式模型，不用旧式。
- 所有金额字段 `Numeric(14,2)`，业务运算用 `Decimal`。
- 不在代码里写死明文密码/密钥，一律从 `.env`。
- 每个新表都有对应 migration（`alembic revision --autogenerate` 或手工）。
- 不引入额外框架（不使用 Celery, Dramatiq, RabbitMQ, Redis）。
- 保持快、小、清晰。宁可功能少而稳，不要功能多而繁。

执行前先通读本 BRIEF，严格按此实施，不自行加戏。
