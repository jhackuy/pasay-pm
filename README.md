# PASay Property Management — V1 第一阶段后端

小型物业管理后端（约 10 套出租房）：FastAPI + PostgreSQL 16，Bearer API Key 认证 + 简单 RBAC，佣金由纯函数引擎计算，全部关键操作写入审计日志。规格唯一来源：`BRIEF.md`。

## GitHub Workflow

- GitHub Issue 是唯一任务 ID 与任务事实源。
- OpenDesign 是设计事实源；生产代码只通过 PR 交付。
- PR 创建后由 CodeRabbit 与 `pasay-gate` 并行提供独立 gate，不能由实现 Agent 自证替代。
- 长期工程规则入口见 `AGENTS.md`，流程与最小 label contract 见 `GITHUB_DEV_WORKFLOW.md`。

## 架构（文字图）

```
Hermes Agent / 客户端
        │  HTTPS: Authorization: Bearer <api_key>
        ▼
   FastAPI (app/main.py) ── 11 个 router（/api/v1/*）
        │  依赖注入: get_current_user (api_key_hash) → role guard
        ▼
   services/  commission_engine.py（金额计算唯一入口）
              audit.py（audit_logs 写入）
        │
        ▼
   SQLAlchemy 2.x ORM ── PostgreSQL 16 (docker compose: db)
        ▲
        │
  alembic upgrade head（api 容器启动时自动执行）
```

- 数据库是唯一真实数据源，金额一律 `NUMERIC(14,2)`（Python 侧 `Decimal`，禁止 float）。
- 时区统一 `timestamptz`；软删除 `deleted_at` 只用于非财务表。
- 附件文件存 `uploads/`，数据库只存路径。

## 快速启动

前置：Docker Desktop（macOS）已运行；本机有 `python3.11+` 与 `pytest`（可选，跑测试用）。

```bash
cp .env.example .env        # 修改 POSTGRES_PASSWORD 等
docker compose up -d        # 启动 postgres + api（entrypoint 自动 alembic upgrade head）
docker compose ps           # 两个服务均 healthy/Up
curl http://localhost:8000/health   # {"status":"ok"}
```

生成第一个管理员 API key：

```bash
docker compose exec api python scripts/create_api_key.py --username admin --role admin
```

把输出的 `API key` 保存好（只存了 SHA-256 哈希，无法二次查看；忘记就用 `--rotate` 轮换）。

## 环境变量（.env）

| 变量 | 说明 |
| --- | --- |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | db 容器初始化 |
| `DATABASE_URL` | SQLAlchemy URL；compose 内被覆盖为 `@db:5432`，本机跑测试/脚本用 `@localhost:5432` |
| `UPLOAD_DIR` | 附件存储目录（默认 `uploads`） |
| `BACKUP_REMOTE` | NAS rsync 目标（默认 `root@192.168.50.27:/volume1/backup/pasay-pm/`） |
| `BACKUP_KEEP` | 备份保留份数（默认 30） |
| `SSH_KEY` | NAS 免密 SSH 私钥（默认 `~/.ssh/pmp_pasay_backup`） |

`.env` 不入库（`.gitignore` 已排除），密钥一律走环境变量，代码不写死任何明文。

## 认证与权限（RBAC）

- 所有 `/api/v1/*` 端点（除 `/health`）需要 `Authorization: Bearer <api_key>`。
- `role ∈ {admin, manager, agent}`：
  - `admin`：全部权限；审批/拒绝/支付/冲销支出；管理佣金规则与确认结算；查审计日志。
  - `manager`：全部业务 CRUD；可确认收入；**不能审批自己创建的支出**（403）。
  - `agent`：只读大部分数据 + 创建 task + 查看自己的佣金结算及关联租约。
- 未授权返回 `403 {"detail": "Insufficient permissions"}`；无效 key 返回 `401`。

## API 端点（前缀 /api/v1）

- `POST /auth` — 校验 key，返回 client info（供 Hermes 探测）
- `GET /health` — 健康检查（不鉴权）
- `properties` / `units` / `tenants` / `leases` — 完整 CRUD（软删除）
- `incomes` — `GET/POST`；`POST /{id}/confirm`；`POST /{id}/reverse`；无 DELETE
- `expenses` — `GET/POST`；`POST /{id}/approve|reject|pay|reverse`；无 DELETE
- `commission/rules` — `GET`（全员）、`POST/PATCH/DELETE`（admin）
- `commission/settlements` — `GET`、`POST`（manager/admin）；`POST /{id}/confirm`（admin）
- `tasks` — `GET/POST`（全员）；`PATCH/DELETE`（admin/manager）
- `tasks/{id}/complete` — 完成任务；recurring 任务自动派生下一条（scheduled，admin/manager）
- `attachments` — `POST`（multipart）、`GET /{id}`、`GET /{id}/download`
- `audit-logs` — `GET`（admin；支持 `table_name` / `record_id` 过滤）
- `reports` — `GET /financial-summary|overdue-rents|monthly|commission|tasks|expenses`（admin/manager；服务端聚合，Hermes 直接取用）

### 第二阶段新增字段

- `tasks`：`recurring`、`interval_months`、`assigned_to`、`completed_at`、`last_completed_at`、`next_due_date`；`status` 增加 `scheduled`
- `leases`：`due_day`（每月几号交租）
- `expenses`：`due_date`（账单到期日）、`unit_id`（可选关联房源）

recurring 任务的 `next_due_date` 由服务端在创建/更新/完成时派生；完成时通过
`POST /api/v1/tasks/{id}/complete` 自动生成下一条（due_date = 上一条 due_date + interval_months）。

财务规则（强制）：收入/支出创建**必须带 status**；已确认收入、已审批/已支付支出禁止改金额；确认/审批后禁止 DELETE，只能 reverse；冲突一律 `409 {"detail": "..."}`。

## 佣金引擎

`app/services/commission_engine.py::compute_settlement(settlement, rule, lease_amount) -> Decimal`：

- `rule_type=percentage`：`lease_amount * value / 100`
- `rule_type=flat`：`value`
- 结果四舍五入到 2 位小数（`ROUND_HALF_UP`）

创建结算时由服务端调用引擎写入 `computed_amount`；API 不接受客户端传入 `computed_amount`（即使传了也会被忽略，金额永远由引擎算出）。这是"AI 不允许直接写账"的核心防线。

## 运行测试

```bash
docker compose up -d db          # 测试需要一个可用的 PostgreSQL
.venv/bin/python -m pytest -q    # 独立测试库 pasay_pm_test，每个用例重建表
```

覆盖：认证/角色、各 CRUD、财务禁止删除与必须带 status、支出审批流转、佣金引擎计算、审计日志。当前用例数 60+，全部绿。

## 备份到 NAS

```bash
./scripts/backup.sh
# 或覆盖配置：
BACKUP_REMOTE="root@192.168.50.27:/volume1/backup/pasay-pm/" BACKUP_KEEP=30 ./scripts/backup.sh
```

流程：`pg_dump -Fc` → gzip → `backups/pasay_pm_<时间戳>.dump.gz` → SSH 流式写入（key `~/.ssh/pmp_pasay_backup`）→ NAS；本地与远程各保留最近 `BACKUP_KEEP`（默认 30）份并清理旧档。
> 说明：本机 NAS（Synology DS920+）对 root 的 `rsync --server` 与 `sftp` 子系统做了限制，因此备份用普通 SSH 通道以 `cat >` 流式写入（backup.sh 已内置）。NAS 免密已验证（`root@192.168.50.27`, `/volume1/backup/pasay-pm/`）。本地自测可用 `BACKUP_REMOTE="/tmp/nas-test"` 覆盖（此时走本地复制）。

## 恢复（含 NAS）

```bash
# 1) 从 NAS 取回备份（SSH 流式，与备份同一通道）
ssh -i ~/.ssh/pmp_pasay_backup -o BatchMode=yes \
  root@192.168.50.27 'cat /volume1/backup/pasay-pm/pasay_pm_YYYYMMDD_HHMMSS.dump.gz' \
  > pasay_pm_YYYYMMDD_HHMMSS.dump.gz

# 2) 解压并恢复（--clean --if-exists 会先清掉同名对象）
gunzip -c pasay_pm_YYYYMMDD_HHMMSS.dump.gz | pg_restore \
  -h localhost -p 5432 -U pasay_pm -d pasay_pm --clean --if-exists --no-owner

# 3) 验证
docker compose exec db psql -U pasay_pm -d pasay_pm -c "\dt"
```

恢复后建议重启 api：`docker compose restart api`（会再次 `alembic upgrade head`，保证 schema 与迁移版本一致）。

## 目录结构

```
app/
  main.py            # FastAPI 入口 + /health
  config.py          # pydantic-settings（.env）
  database.py        # engine / SessionLocal / get_db
  models/            # SQLAlchemy 2.x 声明式模型（12 张业务表）
  schemas/           # Pydantic v2
  api/deps.py        # Bearer 认证 + RBAC 守卫
  api/routers/       # 各资源端点
  services/          # commission_engine.py / audit.py
  core/security.py   # api_key SHA-256 哈希
alembic/             # 迁移（env.py 读 app settings）
scripts/backup.sh    # NAS 备份
scripts/create_api_key.py
tests/               # pytest（独立测试库）
```

> 说明：交付验收清单写"建 11 张表"，实际按 BRIEF 目录结构与 API 清单需包含 `tasks`（`GET/POST/PATCH/DELETE /api/v1/tasks` 是明确要求），因此共 12 张业务表 + `alembic_version`。
