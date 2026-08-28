# PASAY-DEPLOY-78 — BLOCKED Report

| 字段 | 值 |
|---|---|
| Issue | [#78 PASAY-DEPLOY: rebuild deterministic production deployment workflow](https://github.com/pasay/pasay-pm/issues/78) |
| Status | **BLOCKED** |
| Date | 2026-08-28 |
| Executor | TRAE SOLO |
| Branch | `opencode/issue78-20260828083855` |
| Base SHA | `d817c6390cd6053f6431be797e179053b5c66612` |
| Constitution | `AGENTS.md` (Canonical Project Constitution) |
| Decision | **STOP — 拒绝在缺失证据下重建部署工作流** |

---

## §1 STOP CONDITION (HIT)

### 1.1 Issue #78 stop condition（按 PASAY Lead 转述）

```text
STOP if either of the following is true:
  (a) the exact historical deployment contract cannot be recovered;
  (b) a required production credential / environment boundary cannot be proven.
Both clauses were triggered. The implementation halts before any rebuild is attempted.
```

### 1.2 两条停止条件**均被触发**

| 条款 | 触发证据 | 详细见 |
|---|---|---|
| (a) 历史部署合同不可恢复 | `.github/workflows/pasay-deploy-phase1.yml` 已被破坏（ripgrep 分类为 binary）；`.git/objects/` 仅指向一个浅克隆 SHA `d817c639…`；工作树内 `pasay-deploy-phase1` 字面量出现 **0 次**；`HEAD~N:path` 不能被解析。 | §2, §3 |
| (b) 生产凭证/环境边界 in-repo 不可证 | `.env*` 被 `.gitignore` 排除（合规）；`wrangler.toml` 仅声明 secret **名称**（不声明值），值从未入库；GitHub plan tier、Environment、environment-scoped secrets 在仓库内**无任何声明**。 | §4, §8 |

### 1.3 宪法授权依据（SOLO 为何 STOP）

按 `AGENTS.md` §2 第 5 条（Owner-Only Decision Boundary），以下决定**专属于 Owner**，SOLO 不得替代：

> `AGENTS.md:23` — **5. Merge PR、production deploy、Secrets 写入**

按 `project_rules.md` §5.7（Git & Delivery 红线）：

> `project_rules.md:139` — **No production deploy**，SOLO 不部署

两条条款共同构成本次 STOP BLOCKED 的合宪性基础：SOLO 既不能在缺失证据下**重建**部署工作流（这本身就是一种 production-shape 的断言），更不能**执行**任何 `wrangler deploy` / Secrets 写入 / Environment 配置。

---

## §2 Evidence of damage (current file)

### 2.1 工作树中的 `.github/workflows/pasay-deploy-phase1.yml`

`read` 工具直接拒绝读取该文件并返回 `Cannot read binary file`。

> 唯一可被 ripgrep 抽取出来的明文字符行（保留其原始前导空白）为：

```text
          echo "==== END EVIDENCE (secrets/URIs never printed) ===="
```

> ripgrep 在对该文件执行二进制嗅探时**判定其为 binary**，即文件字节流已被破坏到无法直接恢复为有效 YAML 的程度。

### 2.2 字面量 `pasay-deploy-phase1` 在工作树其余位置的检索

```text
$ grep -RIn 'pasay-deploy-phase1' . --exclude-dir=.git
# 结果：0 hits
```

> 即：**没有任何** README、文档、AGENTS、CLAUDE、workflow 配置或源代码引用过该工作流文件名。该文件原本的语义上下文（它的 secret 名、它的 cron 名、它的 concurrency group、它的 SHA pinning、它的 environment name）在仓库内**完全失传**。

---

## §3 Evidence of git-history unrecoverability

### 3.1 `.git/` 物理状态

| 路径 | 状态 | 备注 |
|---|---|---|
| `.git/HEAD` | `ref: refs/heads/opencode/issue78-20260828083855` | `HEAD:1` |
| `.git/refs/heads/opencode/issue78-20260828083855` | `d817c6390cd6053f6431be797e179053b5c66612` | 单一可达 ref |
| `.git/packed-refs` | **不存在** | 无 packed refs |
| `.git/objects/info/` | 空目录 | 无 alternates / shallow 声明 |
| `.git/objects/pack/` | 含 `.idx / .pack / .rev` 1 组 pack 三元组 | 浅克隆（shallow）产物，不含历史 commit blob |

> 唯一可达 SHA 为 `d817c6390cd6053f6431be797e179053b5c66612`；所有 branch / tag ref 在解析后**均回到同一个 SHA**，即没有可回溯的额外 commit 历史。

### 3.2 `HEAD reflog` 仅有 2 条

```text
<sha> checkout: moving from feature/telegram-ui-v2 to opencode/issue78-20260828083855
<sha> clone: from <origin-url> (shallow)
```

> 含义：从 `origin/feature/telegram-ui-v2` → 当前 `opencode/issue78-20260828083855`。`feature/telegram-ui-v2` 这条历史线未被全量拉取；任何对 `pasay-deploy-phase1.yml` 早期版本的 `git show <hash>:.github/workflows/pasay-deploy-phase1.yml` 解析均**不可用**（`read` 不支持 git-ref path，浅克隆亦无 object 解析可达）。

### 3.3 `read` 工具约束

本会话内置 `read` 工具**不接受** `HEAD~N:path` 形式的 git-ref 输入，因此即使本地有更深历史，SOLO 也无法在本机取证得到 `pasay-deploy-phase1.yml` 的历史内容。唯一可行的恢复路径是 Owner 授权**取消浅克隆 + 重新 fetch origin**（见 §8）。

---

## §4 Current production contract (recoverable)

下列证据逐项均**直接引用**仓库内现存文件，按 `path/to/file.ext:LINENO` 标注。

### 4.1 `cloudflare-worker/wrangler.toml`（运行时事实）

```toml
# cloudflare-worker/wrangler.toml:1-3
name = "pasay-cloudflare-worker"
main = "src/index.ts"
compatibility_date = "2026-08-20"
```

```toml
# cloudflare-worker/wrangler.toml:16-25
[[containers]]
class_name = "PasayContainer"
image = "../Dockerfile"
max_instances = 1
instance_type = "basic"

[containers.constraints]
regions = ["APAC"]
```

```toml
# cloudflare-worker/wrangler.toml:35-37
[[durable_objects.bindings]]
name = "PASAY_CONTAINER"
class_name = "PasayContainer"
```

```toml
# cloudflare-worker/wrangler.toml:50-51
[triggers]
crons = ["*/5 * * * *"]
```

```toml
# cloudflare-worker/wrangler.toml:54-63
[[queues.producers]]
  queue = "pasay-events"
  binding = "PASAY_QUEUE"

[[queues.consumers]]
  queue = "pasay-events"
  max_batch_size = 10
  max_batch_timeout = 1
  max_retries = 5
  dead_letter_queue = "pasay-events-dlq"
```

```toml
# cloudflare-worker/wrangler.toml:65-73
# ── Secrets (set via wrangler secret put BEFORE deploy) ──
# Operator must configure ALL of the following before `wrangler deploy`:
#   wrangler secret put TELEGRAM_WEBHOOK_SECRET
#   wrangler secret put PASAY_CONTAINER_INGEST_TOKEN
#   wrangler secret put DATABASE_URL
#   wrangler secret put DATABASE_URL_UNPOOLED
#   wrangler secret put TELEGRAM_BOT_TOKEN
# Missing any above → Worker startup or Container binding fetch fails
# closed (fail-fast per Scope D + ND_RETURN FIX1 blocker #4).
```

> 注：上述 toml 中**无** `[vars]` 表，亦**无** `[env.production.vars]` 表。所有运行时凭据一律走 `wrangler secret put`，**仓库内不存任何 secret 值**。

### 4.2 `Dockerfile`（Container 启动事实）

```dockerfile
# Dockerfile:62-71
ENTRYPOINT ["sh", "-c", "\
set -e; \
if [ -z \"${DATABASE_URL_UNPOOLED}\" ]; then \
  echo '[pasay][fatal] DATABASE_URL_UNPOOLED is required for alembic migrations (Scope E direct/unpooled + ND_RETURN FIX1 blocker #4: NO fallback). Container cannot start.' >&2; \
  exit 1; \
fi; \
export ALEMBIC_DATABASE_URL=\"${DATABASE_URL_UNPOOLED}\"; \
alembic upgrade head; \
exec \"$@\"\
", "entrypoint"]
```

```dockerfile
# Dockerfile:79-80
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
```

> 关键 invariant：**`DATABASE_URL_UNPOOLED` 为强制环境变量**，无 fallback；启动顺序固定为「env check → alembic upgrade head → exec uvicorn」。

### 4.3 `cloudflare-worker/package.json`（构建脚本事实）

```json
# cloudflare-worker/package.json:9-12
"scripts": {
  "build": "wrangler deploy --dry-run",
  "deploy": "wrangler deploy",
  "dev": "wrangler dev",
  ...
```

```json
# cloudflare-worker/package.json:18-26
"dependencies": {
  "@cloudflare/containers": "^0.3.7"
},
"devDependencies": {
  ...
  "wrangler": "^4.124.0"
}
```

### 4.4 `.gitignore`（Secrets 卫生事实）

```ini
# .gitignore:6-8
.env
.env.*
!.env.example
```

> 即 `.env` / `.env.*` 一律被 git 排除，仅 `.env.example`（**非真实值**）可入库。这从仓库机制上**已经**杜绝"凭据随 commit 泄露"的可能性——也正因此我们**永远无法**从仓库内恢复任何凭据值。

### 4.5 `CURRENT_ARCHITECTURE.md` §1（架构冻结事实）

```text
# CURRENT_ARCHITECTURE.md:3
> **ARCHITECTURE_FROZEN = YES**
```

```text
# CURRENT_ARCHITECTURE.md:11-69（节选 ASCII 拓扑首行）
Telegram api.telegram.org
      │
      ▼
┌─────────────────────────────────────────────────────────┐
│  Cloudflare Worker (pasay-cloudflare-worker)             │
      ...
      ▼
                 Neon PostgreSQL 16
```

> 拓扑不变量（取自 §1 / §4 / §5）：Worker 不直接写 Neon、不调用 PTB `run_polling`、不调用 LLM；Queue consumer → Container 仅经 `POST /internal/ingest` + `X-Pasay-Ingest-Token`；2xx ack / 4xx terminal / 5xx & 401 retry。

---

## §5 What Issue #78 acceptance cannot satisfy without improvisation

下表逐条核对 Issue #78 的 9 条 acceptance。**不可满足**项的核心原因均为：缺少 `pasay-deploy-phase1.yml` 的历史形态作为依据。

| # | Acceptance 摘要 | 依赖历史 YAML 的关键事实 | 是否可满足 |
|---|---|---|---|
| 1 | 重建"确定性的"生产部署 workflow | 需要原 YAML 的 step 顺序、job 名称、并发组、trigger 矩阵 | ❌ 必须重建而非恢复 |
| 2 | 隔离凭据；仅引用 secrets 名 | 需要原 secret 名清单（与 §4.1 的 5 个 wrangler secret 名**可能不一致**） | ⚠ 部分可（§4.1 给出 wrangler 侧 secret 名）；但**原 workflow 是否额外引用** GitHub repo / environment secret 名未知 |
| 3 | 可重放：同 input 同 SHA → 同部署 | 需要原 workflow 的 actions pinning / versions | ⚠ 可用 2026 官方推荐版本，但与"原部署"是否 byte-identical 不可证 |
| 4 | 并发安全：同一 ref 不并发部署 | 需要原 `concurrency.group` 命名 | ❌ group 名未知 |
| 5 | 步骤顺序：lint → type → test → build → deploy | Dockerfile entrypoint 已固定 deploy 前序；wrangler `build=dry-run` + `deploy` 分离 | ✅ 可由 §4.2 + §4.3 推得 |
| 6 | Secrets 不入日志 | `wrangler secret put` 模型天然不入库；GitHub `::add-mask::` 可加 | ✅ 可由 §4.1 + §4.4 推得 |
| 7 | 记录部署后的实际 Cloudflare SHA | 需要原 workflow 中"参考 SHA"如何被读取的写法 | ❌ 写入位置 / 标签模式未知 |
| 8 | 失败即停 + 工件上传 | 标准 `actions/upload-artifact` | ✅ 可由 2026 GitHub Actions 默认行为满足 |
| 9 | 不引入轮询 / watcher | Dockerfile CMD 已固定 HTTP only；无 cron step | ✅ 可由 §4.2 推得 |

> **结论**：不可满足/不可证伪的项集中在 #1、#2（环境 secret 名）、#4、#7。任何"rebuild"在这些点上都会**涉及**对未知历史字段的命名决策——这正是 Issue #78 acceptance #1 明文禁止的"无据基础设施臆造"。

---

## §6 What Issue #78 acceptance CAN satisfy from current evidence alone

下列 acceptance **完全可由 §4 的现存证据直接满足**，无需历史 YAML：

- **#5 步骤顺序**：可由 `Dockerfile:62-71`（env check → alembic upgrade head → exec uvicorn）+ `cloudflare-worker/package.json:10-11`（`build: wrangler deploy --dry-run` / `deploy: wrangler deploy`）+ Cloudflare Worker / Container 2026 官方 deploy 顺序（build → push image → deploy worker → roll container）严格推得。
- **#6 Secrets 不入日志**：可由 `wrangler.toml:65-73`（所有凭据走 `wrangler secret put`，**值不入库**）+ `.gitignore:6-8`（`.env*` 一律 ignore）+ 标准 GitHub `::add-mask::` 模式推得。
- **#9 无 watcher / poller**：可由 `Dockerfile:79-80`（CMD 仅 `uvicorn …`，无 `bin/pasay_runtime.py`、无 `getUpdates`）+ `CURRENT_ARCHITECTURE.md:178`（"未实现：Worker 直接写 Neon / Worker 调 PTB / Worker 调 LLM"）直接证明。

> 这三条**本身即可作为一份 PASS-by-evidence 子集**，但**不足以**宣称 #78 整体完成。

---

## §7 OFFICIAL 2026 reference contract (for future rebuild, NON-BINDING)

本节内容仅作**未来重建**时的事实参考；本 PR 自身**不实现、不声明、不启用**任何下列机制。

### 7.1 Cloudflare 官方部署入口（GitHub Actions 侧）

- Action: `cloudflare/wrangler-action@v4`（由 Cloudflare 官方维护，2026 当前 GA）
- 必备 secrets（**仅作占位说明，禁止填入真实值**）：
  - `<CLOUDFLARE_API_TOKEN>` — **占位符**，必须由 Owner 通过 `wrangler login` 或 Cloudflare Dashboard → API Tokens 生成；权限至少 `Workers Scripts:Edit` + `Workers Containers:Edit`（如使用 Container 推送）。
  - `<CLOUDFLARE_ACCOUNT_ID>` — **占位符**，必须由 Owner 在 Cloudflare Dashboard 右侧栏读取。
  - 任何额外 secret 名（例如 `<CLOUDFLARE_DEPLOY_BOT_USER>`）均**不存在于**本仓库当前 `wrangler.toml:65-73` 列出的 5 项之中；若引入，须 Owner 显式批准。

### 7.2 Neon 官方连接约定

- 生产双连接（pooled / unpooled）必须并存：
  - Pooled（运行时）：Neon 控制台连接的 **-pooler** 端点（典型形如 `…-pooler.<region>.aws.neon.tech/neondb?sslmode=require`）
  - Unpooled（迁移）：Neon 控制台的 **direct** 端点（典型形如 `….<region>.aws.neon.tech/neondb?sslmode=require`）
- 强制要求：URL 中包含 `-pooler` 即 pooled；不包含即为 direct。两者**不可混用**——这与 `Dockerfile:64-67` 强制 `DATABASE_URL_UNPOOLED` 用于 alembic 一致。

### 7.3 GitHub Actions 2026 模式（官方）

- `environment:` — 用于把 deploy job 钉到 GitHub Environment；要求计划层级支持（见 §8 第 2 项）。
- `concurrency:` + `cancel-in-progress: false` — 防止同 ref 并发部署的标准模式。
- `permissions:` — 显式最小权限（避免默认 `GITHUB_TOKEN` 过度授权）。
- `::add-mask::<value>` — 在 runner 日志中屏蔽 secret 子串的标准机制。
- `actions/upload-artifact@v4` + `actions/download-artifact@v4` — 2026 当前推荐 pin。

### 7.4 来源

- Cloudflare Workers / Containers 官方部署文档（wrangler-action / External CI/CD via GitHub Actions）
- Neon 官方 pooled-vs-direct 文档（`neon.tech/docs/connect/connection-pooling`）
- GitHub Actions 官方文档（`environment`、`concurrency`、`permissions`、`::add-mask::`）
- GitHub Plans 文档（private-repo Environment feature gate）

---

## §8 Owner-decision checklist

下列决定**专属于 Owner**（`AGENTS.md:23` §2 第 5 条）。SOLO 在本 PR 不假设其中任一项。

1. **是否允许走非 CI 部署路径**：仅靠本地 / Operator 手动的 `wrangler deploy`，**不**用 GitHub Actions；抑或**重建**一份从当前生产证据出发的 workflow？
2. **GitHub 计划层级声明**：当前仓库属于 Free / Pro / Team / Enterprise 中哪一档？这一项直接决定 §7.3 中的 `environment:` + required reviewers + environment-scoped secrets 在**私有仓库**下是否可用。
3. **环境密钥布点核对**：`<CLOUDFLARE_API_TOKEN>` / `<CLOUDFLARE_ACCOUNT_ID>` / `<NEON_PROD_DB_URL>`（**全部为占位符**）当前是 repo secret 还是 environment-scoped secret？二者权限半径不同，必须由 Owner 显式确认。
4. **生产身份核对（仓库内**零**记录）**：以下 ID / Name 均**不存于**本仓库；Owner 必须显式提供至少以下子集才能让任何 workflow 真正可执行：
   - Cloudflare Worker 生产 name（即 `wrangler.toml:1` 已声明的 `pasay-cloudflare-worker` 是否等于生产 worker name）
   - Cloudflare Account ID（`wrangler.toml` 中**没有**该字段）
   - Cloudflare Container class name（即 `wrangler.toml:17` 已声明的 `PasayContainer` 是否等于生产 container class name）
   - Neon Project ID（仓库内**无任何** `neon_project_id` / `project_id` 字段）
5. **授权解浅克隆 + 重新 fetch origin**：以使未来 SOLO 在 Owner-gated Milestone 内可经 `git show <hash>:.github/workflows/pasay-deploy-phase1.yml` 取证恢复历史 YAML。

> 未取得以上 5 项答复之前，**任何**重建动作均构成对 Issue #78 acceptance #1 的违反。

---

## §9 What this PR does NOT do

为对齐 AGENTS.md §2 Owner-Only Decision Boundary 与 §5 历史安全红线，本 PR **明确**未执行：

- ❌ **未** 触碰 `.github/workflows/pasay-deploy-phase1.yml`（保留为已知损坏的 binary 状态）
- ❌ **未** 写入任何 secret 值、任何 API token、任何 DB URL
- ❌ **未** 调用 `wrangler deploy` / `wrangler secret put` / `wrangler login`
- ❌ **未** 配置任何 GitHub Environment、required reviewer 或 environment-scoped secret
- ❌ **未** push tag、触发 release、或任何形式的 production deploy
- ❌ **未** 修改任何其他 workflow / 源代码 / 测试 / 治理文件 / 文档
- ✅ **仅**新增一份 markdown：`PASAY_DEPLOY_78_BLOCKED_REPORT.md`（即本文件）

---

## §10 Next Milestone (Owner-gated)

未来在 Owner 同时满足以下前提时可启动 PASAY-DEPLOY-78 重建 Milestone：

1. Owner 在 §8 第 2 项给出 GitHub 计划层级；
2. Owner 在 §8 第 3 项核对并明示 secret 布点；
3. Owner 在 §8 第 4 项补齐 4 个生产身份（Worker name / Account ID / Container class name / Neon Project ID）；
4. Owner 授权 §8 第 5 项的"解浅克隆 + 重新 fetch origin"，使 SOLO 可**取证**而非**臆造**原 YAML 的 step / secret 名 / concurrency group / SHA 写入位置；
5. SOLO 以"恢复 + 增量改动 diff"形式提交重建 PR，仍受 Issue #78 acceptance #1（禁止无据臆造）约束。

未满足上述任一前提，Milestone **不启动**；本 BLOCKED 状态维持。

---

## §11 Constitution compliance

本 STOP BLOCKED 决定完全对齐 `AGENTS.md`（Canonical Project Constitution）以下条款：

> `AGENTS.md:59` — **All delivery goes through PR; never modify authority or base-branch business code directly.**

> `AGENTS.md:55` — **Git authority and history safety are non-negotiable: no default-branch rewrite, no force push, no shared-history rewrite, no overwriting remote-only commits.**

并与 `project_rules.md` §5.7 一致：

> `project_rules.md:139` — **No production deploy**，SOLO 不部署

SOLO 在历史合同不可证、生产凭证不可证的双重盲区下选择 STOP BLOCKED，正是为了**不**让任何一条上述红线被"agent 自报告 PASS"破坏。Issue #78 重建工作将作为 Owner-gated Milestone 在证据齐全后再启动。

---

**END OF BLOCKED REPORT — TRAE SOLO, 2026-08-28**