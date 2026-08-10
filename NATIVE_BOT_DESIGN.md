# PASay-PM — 原生 Telegram Bot 设计（`pasay-telegram-bot`，Phase 1 产出）

> 只读审计阶段的设计文档（2026-08-10）。基线：git `main` @ `746b605`。本文件不包含任何真实 secret（token/key 均以占位符表示）。
> 背景事实与全链路审计见 `CURRENT_ARCHITECTURE.md`。本文件给出最终目录结构、service 设计、依赖选择、Hermes adapter 接口、callback_data 编码、conversation state、idempotency、切流与测试策略。

---

## 1. 目标与边界

- 新增**原生 Telegram Bot 层** `pasay-telegram-bot`：同仓库、独立 service，是生产 pasay bot token 的**唯一 consumer**。
- Native Bot 负责全部确定性 UI：InlineKeyboard、callback、分页、HTML 卡片 renderer、conversation 状态机、二次确认。
- 财务写操作最终一律调 **Pasay PM API**（`127.0.0.1:8000`）；**禁止直接写 PostgreSQL**。
- Hermes 只做自然语言理解 / 复杂 reasoning / tool 编排；确定性页面不再让 LLM 自由生成。
- 本轮范围：**房源 / 财务 / 逾期 / 收租**四块全量按钮化；维修、佣金、租约编辑只保留入口与现状。

## 2. 技术栈决策：python-telegram-bot v21+（推荐） vs aiogram

**结论：选 `python-telegram-bot`（PTB）≥21（落地时锁定当前 22.x）。**

| 维度 | PTB v21+ | aiogram v3 |
|---|---|---|
| 环境一致性 | 与 Hermes 现有 venv 同库（22.6），概念/调试经验可复用 | 全新 asyncio-only API，无存量经验 |
| 状态机 | 内置 `ConversationHandler`、`CallbackQueryHandler`、`JobQueue`（长轮询内置） | 需自拼 FSM（aiogram 无内置 FSM，官方推荐 aiogram.fsm 或第三方） |
| 成熟度/文档 | 长期维护、文档全、示例多 | 活跃但 API 大版本间破坏性变更多 |
| 与现网排障 | 同库便于对照 Hermes adapter 行为（parse_mode/错误） | 需要额外心智 |

PTB 的 `JobQueue` 依赖 `python-telegram-jobqueue`（随包安装），用 `run_polling()` 驱动，无需额外进程。

**渲染规范（已核实，非模型记忆）**
- Telegram 客户端**原生不支持 Markdown 表格**；当前 Hermes 用 MarkdownV2 + 表格转 bullets 兜底。
- 目标固定为 **`parse_mode=HTML` + 卡片式排版 + InlineKeyboard**：
  - 金额/编号用 `<code>`，标题用 `<b>`，分隔用文本行；不依赖表格。
  - 所有用户/租客/描述文本一律 `html.escape()`，防止实体注入与格式破坏。
  - 消息 ≤ 4096 字符（按 UTF-16 code unit 计），超长自动分页（`/overdue` 每页 5 条 + 分页按钮）。

## 3. 目录结构（新增于仓库根，独立于 `app/`）

```
pasay-telegram-bot/
├── pyproject.toml              # 独立 venv 依赖：python-telegram-bot>=21,<23, httpx
├── pasay_bot/
│   ├── __init__.py
│   ├── main.py                 # 入口：ApplicationBuilder → run_polling()，启动时 getMe 自检
│   ├── config.py               # pydantic-settings：BOT_TOKEN/PASSAY_API_BASE/PASSAY_API_KEY/
│   │                           #   HERMES_API_BASE/HERMES_API_KEY/STATE_DB/HOOK_TOKEN
│   ├── api_client.py           # Pasay API httpx client（Bearer），类型化响应，超时/重试策略
│   ├── roles.py                # update.effective_user.id → 角色（查 Pasay API /users/me）→ 按钮集
│   ├── keyboards.py            # InlineKeyboard 构造 + callback_data encode/decode（§6 单一真源）
│   ├── render/
│   │   ├── __init__.py
│   │   ├── html.py             # escape + 卡片/分页/金额格式化（PHP 千分位、Decimal→str）
│   │   └── cards.py            # rent entry / expense / overdue / financial-summary / unit 卡片
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── commands.py         # /start /menu /help /overdue /summary /properties /incomes
│   │   ├── callback.py         # callback 分发（版本化 CALLBACK 注册表）
│   │   ├── conversation.py     # ConversationHandler：录租金/录支出/二次确认状态机
│   │   └── nl_bridge.py        # 自然语言 → Hermes adapter（§5），结果回 render 输出
│   ├── state/
│   │   ├── store.py            # SQLite 会话状态（§7）
│   │   └── idempotency.py      # 写操作幂等（§8）
│   └── hermes_adapter.py       # Hermes api_server 客户端 + HMAC hook 接收（§5）
├── bin/
│   └── start-native-bot.sh     # launchd wrapper（镜像 start-native-api.sh：env 加载、状态 DB 迁移、exec）
├── tests/
│   ├── conftest.py             # PTB mock Application + 内存/SQLite state + httpx MockTransport
│   ├── test_api_client.py
│   ├── test_render.py          # HTML 转义/卡片/分页/4096 截断
│   ├── test_callback.py        # encode/decode、未知版本拒绝、64B 上限
│   ├── test_conversation.py    # 录租金/录支出/确认流状态机
│   ├── test_idempotency.py
│   └── test_roles.py
├── .env.example                # 见 §4（不提交真实 secret）
└── README.md                   # 运行/测试/切流说明
```

不新建第二套 git 项目；`pasay-telegram-bot` 与后端共用仓库、独立进程（launchd label `ai.pasay.telegram-bot`）。

## 4. Service 设计（部署形态）

- 进程：PTB `Application.run_polling()`，Python 3.11（独立 venv：`pasay-telegram-bot/.venv`，或复用仓库 `.venv` + 新增依赖——**建议独立 venv** 避免污染后端依赖）。
- launchd：`/Library/LaunchDaemons/ai.pasay.telegram-bot.plist`（`RunAtLoad + KeepAlive`，User `jhackuy`，日志 `~/Library/Logs/AI-Agent/pasay-bot.{out,err}.log`）。程序参数 = `bin/start-native-bot.sh`。
- `bin/start-native-bot.sh` 职责（镜像 start-native-api.sh 的 fail-closed 风格）：加载 `.env`（不打印 secret）→ 校验 `STATE_DB` 可写 → `getMe()` 自检（确认 token 有效且无 409 冲突）→ `exec python -m pasay_bot.main`。
- 配置（`.env.example`）：
  ```
  PASSAY_TG_BOT_TOKEN=<production token, 切流后唯一 consumer>
  PASSAY_API_BASE=http://127.0.0.1:8000/api/v1
  PASSAY_API_KEY=<manager 级 key>      # 写操作；reverse 等 admin 动作走角色判定 + admin key 兜底
  PASSAY_ADMIN_API_KEY=<admin 级 key>  # 仅 OWNER 触发的 reverse/pay/commission-confirm 使用
  HERMES_API_BASE=http://127.0.0.1:8642
  HERMES_API_KEY=<API_SERVER_KEY>
  STATE_DB=/opt/pasay-pm/pasay-telegram-bot/state/bot_state.db
  HOOK_TOKEN=<HMAC secret for Hermes→Bot hook>
  ```
- 网络：Bot 只出站（api.telegram.org）与访问本地 `127.0.0.1`；hook 监听 `127.0.0.1:8001`（不绑公网）。
- 消息落点：DM（OWNER/SECRETARY）+ 生产群（按切流结果，§9）。

## 5. 与 Hermes 的 adapter 接口（最薄方案）

### 5.1 入站（自然语言 → Hermes）
Hermes 代码已内置 OpenAI 兼容 API server（`gateway/platforms/api_server.py`，未启用）。启用方式：主 config（及受限 profile）加：

```yaml
gateway:
  platforms:
    api_server:
      enabled: true
      port: 8642
      api_key: <API_SERVER_KEY>   # 生产用强随机值，仅本机可用
```

Native Bot 调用（session 持久化复用 Hermes session）：
- 首选：`POST {HERMES_API_BASE}/api/sessions/{session_id}/chat`，body `{message: "...", session_key: "pasay:<chat_id>:<user_id>"}`。
- 备选（无 session 管理）：`POST {HERMES_API_BASE}/v1/chat/completions` + 头 `X-Hermes-Session-Key: pasay:<chat_id>:<user_id>`。
- 鉴权：`Authorization: Bearer <API_SERVER_KEY>`。
- 返回契约：`{text, structured: {...}?, tool_events: [...]?}` —— Native Bot 只取 `text`/`structured`，交 `render/` 输出；**Hermes 返回的原始 markdown 不直接 sendMessage**（防格式漂移）。

### 5.2 出站（Hermes 主动消息 → Native Bot 渲染）
Hermes cron（如每日 08:00 摘要）/ skill 主动推送，经 webhook/hook 到 Native Bot：
- Native Bot 起 `aiohttp`（或 PTB 无关的轻量 HTTP）监听 `127.0.0.1:8001/hook/hermes`，`X-Hook-Signature: HMAC-SHA256(HOOK_TOKEN, body)`。
- Hermes 侧启用 `gateway.platforms.webhook` 配一条 route（`deliver_only` 或 agent 摘要 job），deliver 到该 hook；或把每日摘要 cron 迁入 Native Bot `JobQueue`（推荐，彻底去掉 LLM 摘要）。
- Native Bot 收到 envelope `{chat_id, text, structured}` 后渲染并 `sendMessage`。

### 5.3 失败语义
- Hermes 超时/5xx：Bot 回「稍后再试」，不渲染半成品；NL 路径所有**写操作一律仍走确定性确认流**（§8），Hermes 输出只读信息。

## 6. callback_data 编码方案

- 上限：**64 bytes**（Telegram 硬限制）。
- 格式：`v1:<action>:<entity>:<id>:<nonce>`，全部小写 ASCII + 数字，`: ` 分隔，例：
  - `v1:cnf:inc:42`            确认收入 #42（confirm income）
  - `v1:rv:inc:42`             冲销收入 #42（reverse income，仅 OWNER 显示）
  - `v1:apv:exp:7` / `v1:rej:exp:7`   审批/拒绝支出 #7
  - `v1:pg:ovd:2`              逾期列表第 2 页
  - `v1:men:prop`              回房源菜单
- 规则：
  - `keyboards.py` 是 encode/decode 的**单一真源**；decode 校验版本前缀，未知版本/非法 id → 忽略并 `answerCallbackQuery("已过期")`。
  - 每份确认卡生成时附带 `nonce`（幂等键短码，§8），防双击/重放；卡片 15 分钟过期。
  - 不用 JSON（超长）、不用 base64（无必要）、不带中文（节省字节）。

## 7. conversation state 存储

- 方案：**本地 SQLite**（`STATE_DB`，与 Hermes 的 `state.db` 同模式理念，零运维、可随 launchd 重启保留）。
- 表：
  ```sql
  conversations(chat_id TEXT, user_id TEXT, state TEXT,
                payload_json TEXT, updated_at TEXT, expires_at TEXT,
                PRIMARY KEY(chat_id, user_id));
  idempotency_keys(key TEXT PRIMARY KEY, kind TEXT, resource TEXT, status TEXT,
                   result_json TEXT, created_at TEXT, expires_at TEXT);
  ```
- 群聊用 `(chat_id, user_id)` 复合主键防串话；DM 用 `(chat_id, user_id=chat_id)` 归一。
- TTL：确认类状态 15 分钟，未确认自动作废并提示重录；查询类（分页）无状态（callback 自带页码）。
- 可选升级 Redis（多副本/横向），当前单机 SQLite 足够，不做预优化。

## 8. Idempotency 方案

后端当前**无 idempotency key、income 无唯一约束**（审计 P3），因此双层兜底：

1. **Bot 层（本轮必须）**
   - 每次写动作生成幂等键 `ik:<action>:<nonce>`（nonce = 卡片生成时随机 8 字节 hex）。
   - 流程：写前 `SELECT idempotency_keys WHERE key=?`：
     - `status=done` → 直接回显 `result_json`（幂等重放，不重复调用 API）；
     - `status=in_flight` → 提示「处理中」并忽略；
     - 无 → 标记 `in_flight` → 调 Pasay API → 成功标记 `done`（存结果）→ 失败标记 `failed`（允许重试）。
   - 配合 callback nonce：同一卡片按钮第二次点击直接忽略。
   - 双击 confirm 场景：Bot 先本地幂等挡住；即便穿透，后端 `409 "Only pending income can be confirmed"` 由 Bot 捕获后当「已确认」刷新卡片，不报错吓用户。
2. **后端加固（后续阶段小改，本轮不改代码）**
   - `incomes` 增加 `idempotency_key VARCHAR(64) NULL UNIQUE`；或部分唯一索引 `(lease_id, 期间)`（期间从 description 提取或新增 `rent_period` 列）。
   - `confirm` 增加并发安全：`UPDATE incomes SET status='confirmed' WHERE id=? AND status='pending'`（受影响行数=0 → 409），消除读-改-写竞态。

## 9. 切流（scoped cutover）方案

原则：**每阶段可回滚；每个 token 只保留一个 consumer；先只读、后写、再停 Hermes polling**。

| 阶段 | 动作 | 验证/回滚 |
|---|---|---|
| 0 预备 | 停 `com.ai-controller.bot`（`launchctl bootout system/com.ai-controller.bot`，先与用户确认 ai-controller 是否仍需要） | 主 token 409 消失（`grep -i conflict` 于 gateway 日志）；如误停 `bootstrap` 恢复 |
| 1 只读并行 | 部署 Native Bot（`ai.pasay.telegram-bot.plist`），**同一生产 token**（先接主 @zhushoumacbot 或按用户决定接受限 @pasayhousebot）；Hermes 先暂停 polling（plist `Disabled=true` 或 profile gateway stop）；Native Bot 只启用查询命令（/overdue /summary /properties）+ 卡片 | 无 409；`getMe` 一致；群内消息不重复；回滚=恢复 Hermes polling、停 Bot |
| 2 写操作切换 | 按功能切：录租金→confirm、录支出→审批/支付、reverse（仅 OWNER）。Hermes skill 同步禁用对应敏感 tool（或改只读 skill 副本） | 双击无重复入账（§8）；审计日志 actor 正确（Bot 用独立 manager key，actor=key 对应用户） |
| 3 群路由收敛 | 生产群 -1004433994558（@pasayhousebot 群）与 -5417146216（主）按 owner 决定：一 bot 一 token 一群；另一 gateway 停止或改 token | 群消息只由 Native Bot 回 |
| 4 收尾 | 清理：Hermes 对应 profile/主 gateway 的 telegram platform 停用；`channel_directory` 收敛；cron 摘要迁 Bot JobQueue；文档更新 | 长跑 24h 无 409/丢消息 |

- 风险点：切流期间旧 LLM 卡片与新按钮并存 → 阶段 2 前在群里公告「写操作请用按钮」；Hermes 摘要 cron 在迁走前保持现状（只读）。
- 生产 token 与 `PROPERTY_API_KEY` 分离：Bot 用 manager key（+ admin key 兜底），Hermes 用 manager key，admin key 仅运维（审计 P5 一并解决）。

## 10. 测试策略

- **单元**（`pasay-telegram-bot/tests/`，沿用仓库 pytest，不引新框架）：
  - `test_callback.py`：encode/decode 往返、64B 上限、未知版本拒绝、nonce 幂等。
  - `test_render.py`：HTML 转义（`<>&"`、用户输入注入）、金额千分位/Decimal、4096 截断、分页页脚。
  - `test_api_client.py`：httpx `MockTransport` 模拟 200/401/409，确认 409→「已确认」语义。
  - `test_conversation.py`：录租金状态机（pending→确认→done / 超时作废 / 取消）。
  - `test_idempotency.py`：in_flight/done/failed 三态转移、双击重放。
  - `test_roles.py`：5177241442→OWNER、1083657401→SECRETARY、未知→拒绝敏感动作。
- **集成**：PTB 无网络模式（`Application.builder()` + 注入 fake `Update`）驱动 handler；后端用 `pasay_pm_test` 测试库（沿用 `tests/conftest.py` 模式）验证 Bot→API 全链路写路径。
- **E2E**：沿用 PHASE2 的 computer_use 方式在真实群验证卡片/按钮/回调/4096 分页/双击。
- **CI 门禁**：`pytest pasay-telegram-bot/tests` + `pytest tests`（后端 102 个）全绿才合入。

## 11. 后端配合改动清单（后续阶段，本轮不改）

1. `alembic/versions/xxx_bot_support.py`：`users.telegram_user_id BIGINT UNIQUE NULL`（+ 可选 `display_lang`）；角色仍用现有 `user_role` 枚举。
2. `incomes`：`idempotency_key VARCHAR(64) NULL UNIQUE`；`confirm`/`reverse` 改条件 UPDATE 防并发（§8）。
3. 新增只读端点（可选）：`GET /api/v1/users/me`（供 Bot 角色解析）、`GET /api/v1/incomes?lease_id=&status=`（分页查询，避免 Bot 拉全表）。
4. 运维：`scripts/create_api_key.py --role manager` 生成 Bot/Hermes 专用 key；`roles.json` 内容迁入 DB 后作为历史文档保留。
5. 文档：README router 数（11→12）与架构图随本阶段提交一并修正。

## 12. 依赖与版本锁定

- `python-telegram-bot>=21,<23`（落地锁定 22.x，与 Hermes venv 一致，便于对照排障）。
- `httpx>=0.27`（api_client / hermes_adapter）；`pydantic-settings`（config，与后端一致）；`aiosqlite` 或标准库 `sqlite3`（state，标准库即可，零依赖优先）。
- 不引入 aiogram / redis / celery / docker（单机 launchd 足够）。
