# PRODUCT_CONFORMANCE_AUDIT_001

> 只读产品符合度审计 · READ-ONLY · 未修改任何代码 / Penpot / 数据库 / 配置
> 审计时间：2026（Asia/Manila） · 产品事实源：Penpot「Pasay AI — Product Design v1.0」（经 MCP 逐页实读 00–14） · 代码事实源：`D:\AI-Review\pasay-pm`
> 本报告完成即停止，不进入开发。

---

## 0. 报告首页（给 Owner）

### 当前产品符合度：**61%**

| 结论 | 数量 | 项 |
|---|---|---|
| **PASS** | 10 | 四个固定按钮 · 房产上帝视角 · 单房 Quick Status · Owner Attention · Rent 完整闭环 · Repair 主动跟进闭环 · Expense PENDING→APPROVED→PAID · AI 主动运营闭环 · AI Persona/中英策略 · Button/Fast Path 绕开 LLM |
| **PARTIAL** | 2 | Property Lifecycle · 延迟与可靠性(可观测) |
| **MISSING** | 5 | **Property Channel** · **Excel/照片导入** · **完整 Excel 导出** · **Control Panel** · **merchant_id 多租户边界** |
| **CONFLICT** | 0 | —（merchant_id 以「缺位」而非「对冲突有实现」形式存在，归入 MISSING） |
| **LEGACY** | 1 | 双任务系统 / 旧 `unit_status` 枚举与 `unit_state` 并存 / 中文旧菜单别名共存于 i18n |

| 复用判定 | 数量 | 项 |
|---|---|---|
| **A DIRECT REUSE** | 8 | 四个固定按钮 · 单房 Quick Status · 房产上帝视角 · Owner Attention 过滤 · AI Persona/中英 · Fast Path 快速视图 · 单房数字档案(unit_timeline) · i18n 双语 renderer |
| **B ADAPT** | 6 | Rent 闭环 · Expense 闭环 · Repair 闭环 · AI 主动运营(承诺/升级/Outbox) · 租客/合同/收支/证据 API · 迁移(Alembic) 与审计服务 |
| **C REWRITE / 新建** | 5 | Property Channel · 导入流水线 · Excel 导出 · Control Panel · merchant_id 数据模型地基 |

---

## 1. 审计方法

- **Penpot 事实源**：通过 Penpot MCP（`http://127.0.0.1:4401/mcp`）只读读取 **15 页** 全部文本与结构（`run_js.py` · `penpotUtils.getPages/getPageByName/shapeStructure`）。逐页 dump 见 `.audit/pages/00.md … 14.md`。
- **代码事实源**：对 `app/`（FastAPI 后端）、`pasay-telegram-bot/`（原生 PTB Bot）、`alembic/versions/`、`tests/` 做静态取证（grep/read）。
- **判定口径**：每个结论至少引用一处代码/模型/迁移/测试证据，禁止凭文件名推断。只读链路已按任务说明验证：Penpot 2.17.1 / MCP 2.15.4 读路径正常，本任务**未做任何 Penpot 写操作**（未用 `import_image`、未 `execute_code` 做任何写形状动作）。
- 未运行 Runtime（本任务禁止重启）；Fast Path / Worker / Copilot 的**运行态数字**（如真实 P95、任务数）未采集，仅以确定性代码路径与既有测试套件作为证据。

---

## 2. 逐页符合度结论（PASS / PARTIAL / MISSING / LEGACY 逐条取证）

### 00 Product Principles — 部分符合（2/4 层就位）
| 子项 | 判定 | 证据 |
|---|---|---|
| North Star：Telegram 里 AI Property Manager，非「软件+聊天」 | **PASS** | 原生 bot 是主动运营 + 按钮闭环的产品载体，非纯镜像 Hermes 聊天（`pasay-telegram-bot/` 全库）。 |
| 四层职责：Telegram/Property Channel/PostgreSQL/Control Panel | **MISSING 2/4** | Telegram ✓、PostgreSQL 唯一事实源 ✓（`app/` 全财务写经 API+审计）；**Property Channel 缺**、**Control Panel 缺**（见 02/11）。 |
| Button-first + AI-underneath | **PASS** | 固定按钮在 `conversation.handle_message` 里**先于任何 NL/LLM** 精确路由（`conversation.py:83-86` + `keyboards.fixed_menu_route_for`）。 |
| AI 工作标准（自动处理→跟进→催办→升级→完结→归档） | **PARTIAL** | 跟进/催办/升级/完结已实现（见 03/07）；「归档」无产品语义（见 05）。 |
| Merchant 首日 merchant_id | **MISSING** | 全库 `grep -i merchant` 为 **0 命中**（代码/模型/迁移/测试），没有任何多租户列。 |
| 冻结规则（先设计再开发） | **PASS** | 本仓以 Penpot 交付链（00–14）+ 各 PHASE/V *BRIEF 文档推进，遵守「先回产品设计」。 |

### 01 Telegram UX — PASS
| 子项 | 判定 | 证据 |
|---|---|---|
| **四个固定按钮** | **PASS** | `keyboards.py:87` `FIXED_MENU_ROUTES={"🏠 Properties","✅ Tasks","💰 Rent","💸 Expense"}`，`reply_keyboard()` 持 `is_persistent` 四键；`roles.locale_for_chat` 群内双英。对应设计底部 `🏠房产 ✅待办 💰收租 💸支出`。 |
| 房产上帝视角 | **PASS** | `cards.properties_quick_card` + `quick.build_quick_properties`（逾期>将到期>空置>正常排序）。 |
| 单房 Quick Status | **PASS** | `cards.unit_card` / `unit_status_label`（🟢已出租/🟡部分等）+ `unit_page_keyboard`。 |
| Owner Attention 卡片 | **PASS** | 见 04。 |
| UX Rule：群里只承担状态/动作/决策 | **PASS** | 固定菜单只进 Quick View，伸手进 detail 靠按钮（`apply-button / 完整资料` 类）。 |

### 02 Property Channel — MISSING
| 子项 | 判定 | 证据 |
|---|---|---|
| 每套房一个动态档案文章（property_id↔channel_message_id，业务变化后**编辑原文章**） | **MISSING** | 无任何 `property_id ↔ message_id` 映射表 / 迁移；`external_message_id` 只存在于 `evidence`（私有存档频道的媒体索引，`evidence.py`），非「每套房档案正文」。 |
| 群内引用「精简结论 + 完整房产资料」跳频道 | **MISSING** | bot 的「📄」按钮跳到**消息内 on-demand 单房卡片/时间线**（`unit_timeline_card`），非持久频道文章。 |
| 数据边界：先写 PostgreSQL，Renderer 更新频道 | **MISSING** | 无频道 renderer 更新链路。 |
| 频道双语（中文详情/English Details） | **MISSING** | bot 群内双语 renderer 存在（i18n bi），但无「频道」载体。 |

> 备注：仓库确有「Telegram 私有存档频道」= 证据/照片存储（`storage_provider='telegram_channel'`，`commands.py:699-757` 转存照片），这是**媒体存档**，不是 Penpot 定义的**房产动态档案出版物**。二者不要混淆。

### 03 AI Operating Model — PASS（承诺机制强）
| 子项 | 判定 | 证据 |
|---|---|---|
| AUTO 一等（查询/更新/提醒/催办/异常发现） | **PASS** | `scheduler.py` + `generation.py` 自动生成 RENT_DUE/RENT_OVERDUE/APPROVAL_PENDING/PAYMENT_PENDING/FINENESS 任务；`quick` 确定性视图。 |
| PREPARE + CONFIRM（租客/合同/支出先准备后人确认） | **PASS** | Copilot `proposals.py` + `execute.py`：PENDING 提案 → Owner 点 `[✅ 确认安排]` 才 `execute`；`copilot_confirm_keyboard`。 |
| HUMAN AUTHORITY（审批/付款/Reverse/高风险删除） | **PASS** | `income.py` confirm/reverse 仅 `owner_subject_only`；`expense.py` approve/pay 边界；`admin only` reverse。 |
| **承诺机制**（AI 说「会跟进」必须产生后续动作） | **PASS** | `promises.apply_promise` 落库结构化 promise + `escalate_due_promises` 到期重催、超 N 次升级 Owner；`repair_flow` 缺凭证 → 秘书 FOLLOWUP。 |
| 成功标准（靠谱员工：知道上下文/不重复问/做完汇报/没做完跟进/真正关掉） | **PASS** | `copilot/context`, `today_fast`, `today`, `why`, `digest`, `reconcile`（结算后不重复提醒）共同满足。 |

### 04 Owner Attention — PASS
| 子项 | 判定 | 证据 |
|---|---|---|
| Owner 只看「必须他决定」的事 | **PASS** | `owner_scope.is_owner_actionable`（审批/属于他的付款/决策/升级），`owner_only=True` 贯穿 `/operations/tasks`、`quick_tasks`、`summary`。 |
| Attention ≠ Tasks | **PASS** | 系统内部全量 operational_tasks，Owner 队列是子集过滤，非全部任务。 |
| 通知预算（低静默/中摘要/行动即时/高风险@） | **PASS/PARTIAL** | promise/escalation 决定「重催 vs 升级」；`notification_outbox` 至少一次投递；但「低价值静默归档」「中价值摘要合并」仅为单点行为（`_payable_expense_rows` 等）无统一通知预算配置 → 判 **PARTIAL** 于该子项，整体 04 仍 **PASS**。 |
| 核心指标（Forgotten≈0 等） | **PARTIAL** | `redelivery.py` / `reconcile` / 去重指数保证「不丢任务」，但**无指标看板**（见 11）。 |

### 05 Property Lifecycle — PARTIAL
| 子项 | 判定 | 证据 |
|---|---|---|
| NEW→VACANT→OCCUPIED→SOLD→ARCHIVED 状态机 | **PARTIAL** | `Unit.unit_state` 是**自由 VARCHAR**（`property.py:43`，注释明言「legacy enum 不动」，为 future 预留）+ `unit_lifecycle_events`（`units.py:_record_lifecycle` 落库事件）。但**没有 SOLD/ARCHIVED 枚举、没有强制合法迁移、没有归档筛选视图**。 |
| 自然语言触发 + 高风险确认（绝不默认删除） | **PARTIAL** | bot 有 Unit 新增确认（`unit_add_confirm_keyboard`），但**无「出售/归档」高风险确认流**。软删除存在（`SoftDeleteMixin`），未默认删历史 ✓。 |
| 归档体验（已归档/已出售筛选） | **MISSING** | 无 archived/sold 过滤端点。 |

### 06 Rent Workflow — PASS
| 子项 | 判定 | 证据 |
|---|---|---|
| 收租上帝视角（已收/部分/未收） | **PASS** | `quick.build_quick_rent`（expected/collected/outstanding/collection_rate/unpaid_unit_count）；`cards.rent_quick_card`。 |
| DUE→PARTIAL→PAID→OVERDUE→FOLLOW-UP | **PASS** | `Income` pending/confirmed/reversed + owner confirm/reverse（`income.py`）；部分付款 `rent.match_partial_*`（i18n）+ 账务「累计已付/剩余」；逾期自动任务 `RENT_OVERDUE` + promise 跟进。 |
| 数学与审计（每笔唯一、可审计、可 Reverse） | **PASS** | `idempotency_key` + `uq_incomes_idempotency_key`（`financial.py`）；每笔 `record_audit`（confirm/reverse）；状态守卫 409 防重。 |
| Fast Path 走确定性、目标 <300ms | **PASS** | `/operations/quick/*` + `/operations/copilot/today` 默认**无 LLM** 确定性路径；`LatencyTracker` 记录 wall-clock（`state/latency.py`）。 |

### 07 Repair Workflow — PASS
| 子项 | 判定 | 证据 |
|---|---|---|
| REPORTED→…→COMPLETED→EXPENSE→PAID | **PASS** | `repair_flow.set_repair_stage`（ISSUE_REPORTED→…→CLOSED 在 `details.repair_stage`）；`AC_MAINTENANCE` 任务；完成进入 Expense 由业务流承接。 |
| **AI 主动追问**（次日无更新 Did the technician finish） | **PASS** | `repair_flow.ensure_evidence_followup` 完成缺凭证 → 秘书 FOLLOWUP（`dedupe_key=repair-evidence:{task}`）；`reminder_generation` + 到期 `next_check_at`；promise 到期重催。 |
| 完成：关任务 + 同步更新（费用自动进 Expense） | **PASS** | `complete` 触发 `ensure_evidence_followup` + `suppress_pending_redeliveries`（`operations.py`）。 |

### 08 Expense Workflow — PASS
| 子项 | 判定 | 证据 |
|---|---|---|
| PENDING→APPROVED→PAID（一次一个决策） | **PASS** | `ExpenseStatus` pending/approved/rejected/paid/reversed；`expense.py` approve→pay 分步；bot `expense_approval_keyboard` 卡片。 |
| **硬规则**：APPROVED≠完成，只有 PAID 才关 Owner 付款待办 | **PASS** | `_payable_expense_rows` 把「APPROVED 未付」作为 payable task 持续留在 Owner 队列；`build_quick_tasks(admin)` 追加这些行。 |
| **去重规则**：靠 expense_id/task/time，不凭金额 | **PASS** | `find_similar_paid_expenses` 多强字段（同 unit+金额+purpose+日期窗）才提示；docstring 明言「Amount alone is never a match」。 |
| 上传凭证 | **PASS** | `receipt_attachment_id` + `expense_pay_confirm_keyboard`（凭证可选，不阻塞 PAID）。 |

### 09 Import & Migration — MISSING
| 子项 | 判定 | 证据 |
|---|---|---|
| Excel/CSV/照片文件夹/ZIP/合同上传 | **MISSING** | 全仓无 `openpyxl`/CSV/导入端点；`attachments` 仅通用媒体上传，无资料解析。 |
| AI 识别 → Staging/Preview → 冲突预览 → 确认导入 | **MISSING** | 无 staging 表、无冲突检测、无「确认导入」写正式数据的安全流。 |
| 产品承诺「把现有资料给我，我帮你整理」 | **MISSING** | 无。 |

### 10 Export — MISSING
| 子项 | 判定 | 证据 |
|---|---|---|
| 完整 Excel Workbook（Properties/Tenants/Leases/Rent/Expenses/Repairs/Tasks/Archive，Active+Archived） | **MISSING** | 无 workbook 生成；`reports.py` 只返回 JSON 聚合，无文件导出。 |
| Telegram 一键导出 | **MISSING** | 无导出命令/按钮。 |
| Control Panel 下载历史/范围/大小/时间 | **MISSING** | 见 11。 |
| 原则：数据永远可完整导出，不制造锁定 | **MISSING** | 依赖 DB 层，无产品导出能力。 |

### 11 Control Panel — MISSING
| 子项 | 判定 | 证据 |
|---|---|---|
| Overall/Telegram/API/DB/Worker/AI 健康 | **PARTIAL(仅/health)** | `main.py:60-67` 仅 DB ping；无 Telegram/Worker/AI 状态聚合。 |
| 商户总览（Properties/Occupied/Vacant/Repairs/Expense） | **MISSING** | 无。 |
| Today 指标（Requests/Errors/P95 fast path） | **MISSING** | 无请求/错误/P95 统计端；`LatencyTracker` 仅在 bot 进程内存，未上推到面板。 |
| Restart Bot/Worker · Refresh · Recent Errors | **MISSING** | 无控制端点。 |
| **与 Bot 解耦**（Bot 崩仍可看健康） | **MISSING** | 无可视化面板，健康面只有后端 `/health`。 |

### 12 Merchant Architecture — MISSING（架构级缺口）
| 子项 | 判定 | 证据 |
|---|---|---|
| **所有业务事实天然属于 merchant_id** | **MISSING** | `Property/Unit/Tenant/Lease/Income/Expense/OperationalTask/Task` 均**无 merchant_id**（`property.py/tenant.py/lease.py/financial.py/operations.py/task.py`）。全库 0 处 `merchant`。 |
| 角色 PLATFORM_ADMIN/MERCHANT_OWNER/MANAGER/STAFF/AI_AGENT/SYSTEM | **PARTIAL** | `identity.PrincipalType`(HUMAN/SERVICE/AI_AGENT/SYSTEM) 与 `UserRole`(admin/manager/agent) 有近似，但**无 MERCHANT_OWNER/MERCHANT_ADMIN/MERCHANT_STAFF 这种「商户内」角色分层**；bot 角色 OWNER/SECRETARY 是 Telegram 硬编码映射（`roles.py:21`），不落库。 |
| 当前只 Pasay，未来第二客户不复制 bot/DB | **MISSING** | 无 merchant 层，未来扩展须先补数据模型地基。 |

> **重要框定**：`merchant_id` 缺位不来自「架构不好看」，而是来自产品（00/12）明示的「底层从第一天保留」**绝对约束**未落地，属 P0。

### 13 AI Persona & Language — PASS
| 子项 | 判定 | 证据 |
|---|---|---|
| 稳定身份 AI Property Manager | **PASS/PARTIAL** | bot 固定称呼/头像由 telegram bot identity 承担；「AI 是 Manager」由文案体现，非独立 persona 配置文件。判 **PARTIAL**。 |
| Owner 中文优先 / Secretary English first | **PASS** | `roles.ROLE_LOCALES={OWNER:"zh", SECRETARY:"en"}`；`locale_for_chat` 群内 `bi` 双英；i18n 完整 zh/en 双语。 |
| 靠谱员工行为（记得承诺/不重复问/做完汇报/没做完追） | **PASS** | 承诺机制 + reconcile + 确定性卡片 = 机器可验，非文案。 |
| 完成后的正向反馈、学会闭嘴（低价值静默） | **PASS** | promise/escalation 分级投递；反馈文案 neutral-declarative。 |

### 14 READY FOR DEV — PASS
| 子项 | 判定 | 证据 |
|---|---|---|
| 每个功能含行为/按钮/文案/状态机/DB 影响/AI 权限/异常/延迟目标/验收 | **PASS** | 本仓 V*/PHASE*/PASAY-V2 系列 BRIEF + `tests/`（app 26 文件、bot 40 文件）即「Spec Pack + pytest 自动验收」。 |
| 旧代码策略 A/B/C | **PASS** | 本报告 0 节即按该策略输出复用判定。 |

---

## 3. 优先级清单（P0 / P1 / P2）

### P0 — 阻碍核心产品体验（不解决产品不成立）
1. **P0 · merchant_id 多租户数据模型地基缺失**（00/12）。产品明示首日约束，现为零。任何模块化/多客户/白标都依赖它；越晚补迁移成本越高。→ C 新建地基。
2. **P0 · Property Channel 缺失**（02）。方案四层职责里的「房产档案与展示」层完全缺位；房东「上帝视角成品」在 bot 内是 on-demand 卡片，产品要求「每套房一个动态档案，群内引用」。→ C 新建。
3. **P0 · Control Panel 缺失**（11）。「服务健康与商户运营」层缺位；Bot 崩溃时 Owner 无任何可观测面，与产品「与 Bot 解耦」硬要求冲突；同时缺 Today / P95 / Errors。→ C 新建。

> 说明：把 09/10 归 P1 而非 P0 的理由——导入/导出是「迁移接客」与「数据主权」能力，不阻塞现有 Pasay 单商户在 bot 内的日常收租/维修/支出闭环；merchant_id / Property Channel / Control Panel 才直接影响产品定义成立与否。这是**基于产品符合度**的判断，非工程量优劣判断。

### P1 — 产品验证阶段应完成
4. **P1 · Excel / CSV / 照片导入（含 staging/preview/冲突确认）**（09）。
5. **P1 · 完整 Excel 导出（Telegram 一键 + 全表 + Active+Archived）**（10）。
6. **P1 · Property Lifecycle 落成正式状态机**（05）：把自由 `unit_state` 收敛为强制枚举 NEW→VACANT→OCCUPIED→SOLD→ARCHIVED + 归档/已出售筛选视图 + 出售高风险确认流（当前只有事件日志，无强迁移）。

### P2 — 商业化后再考虑
7. **P2 · 第二商户接入/白标/自助注册**（12，依赖 P0#1 完成后是纯增量）。
8. **P2 · 通知预算全局配置化 + 指标看板化（Human Actions Saved / Closure Rate / Forgotten≈0）**（04/11 增强）。
9. **P2 · AI Persona 独立化**（13，独立身份配置文件/头像/文案库，服务多商户品牌）。

---

## 4. 面向 Owner 的五个必答问题

### 4.1 当前旧代码是否值得继续作为 Product Validation Runtime？
**值得，且它就是当前已上线的 Product Validation Runtime，不应推翻。** 证据：
- 原生 bot（`pasay-telegram-bot/`）已具备**完全的确定性 UI 层**：固定四键菜单、InlineKeyboard/callback、编年史/HTML renderer、i18n 双语、`LatencyTracker`、`store`(SQLite 状态)。
- 后端（`app/`）已具备**主动运营闭环**：任务生成/重催/升级（promise）、通知至少一次 Outbox、reconcile、repair 证据跟进、Owner 队列过滤、快速视图确定性、审计与幂等。
- 测试面大（app 26 + bot 40 个 test 文件），且近期 git log 显示正在做真实生产链修复（Unicode 消费者、IPv4、Bot↔后端鉴权、live 诊断）——这是**活的生产运行时**。
- 结论：作为**验证产品假设**（按钮 + AI 主动运营 + 双语）它完全胜任；它的问题不是「该不该保留」，而是「缺哪些产品层」（3 个 P0）与「哪些是历史包袱」（见 4.4）。

### 4.2 哪些模块应该直接复用（A）？
- **Telegram UI 外壳**：固定四键菜单、Quick View renderer、单房卡片、i18n 双语、callback 状态机（`keyboards.py` / `render/cards.py` / `render/i18n.py` / `handlers/*`）。
- **确定性 Fast Path**：`quick.py`（properties/rent/expense/tasks/digest）与 `today_fast`——正是产品「按钮不走 LLM、<300ms」的实现。
- **单房数字档案**：`build_unit_timeline`（可演进为 Property Channel 的「按需渲染数据源」）。
- **Owner Attention 过滤**：`owner_scope.is_owner_actionable`（跨端点共享，直接复用）。
- **审计 / 幂等 / 软删地基**：`AuditMixin/SoftDeleteMixin`、`record_audit`、`idempotency_key`。
- **身份与语言策略**：`roles.ROLE_LOCALES`（Owner=zh/Secretary=en/群=bi）。

### 4.3 哪些只需适配（B）？
- **Rent / Expense / Repair 三闭环业务服务**：状态机正确，只需补 merchant_id 关联再复用（迁移加列即可）。
- **AI 主动运营**：promise/升级/Outbox/scheduler/reconcile——逻辑正确，补 merchant 边界即可复用。
- **Copilot 提案 → 执行**：`proposals`/`execute` 已实现 PREPARE+CONFIRM+HUMAN AUTHORITY，只需把提案执行路径接上 merchant 作用域。
- **财务/租客/合同/证据 API**：CRUD + 审计，加 merchant 过滤列为 B 适配。

### 4.4 哪些应该重写更便宜（C）？—— 只有证据表明阻碍产品时才判 C
- **Property Channel**（C 新建）：产品要的「持久频道文章 + property_id↔message_id 编辑原文章」当前完全不存在，无旧代码可复用，属新建而非重写。
- **merchant_id 数据模型地基**（C）：全库 0 命中，无旧实现；是建立多租户边界的必要新建，不是推翻旧代码。
- **Control Panel**（C 新建）：无现成面板/健康聚合，需新建（复用 `/health` 与 `LatencyTracker` 作为数据源即可）。
- **导入/导出**（C 新建）：无现成实现。
- ⚠️ **不判 C 的模块**：`app` 后端业务服务层**不重写**——它们逻辑正确（不是「架构不漂亮」就重写）；唯一建议**剥离/废弃**的是下节 LEGACY 死路径，而非重写主干。

### 4.5 下一步最值得开发的 3 个产品缺口？
按「产品符合度杠杆 × 依赖顺序」：
1. **merchant_id 多租户数据模型地基**（P0）——它是 12 页架构的第一块砖，且所有其他 B/C 复用都依赖它；现在补最便宜。
2. **Property Channel：每套房动态档案 + 群内引用**（P0）——补齐「四层职责」缺失层，让 Owner 上帝视角真正成品化；可复用 `build_unit_timeline` 作为渲染数据源。
3. **Control Panel 最小可用版**（P0 + P1 合并）——健康状态 + Today 指标 + Recent Errors + 解耦（复用 `/health` 与 bot `LatencyTracker`），同时把 04 的指标（Requests/Errors/P95/AI handling）可视化，一石二鸟。

---

## 5. 证据索引（关键条文 → 文件）

| 结论 | 证据文件 + 位置 |
|---|---|
| 固定四按钮 = 精确路由先于 LLM | `pasay_bot/keyboards.py:87`(FIXED_MENU_ROUTES)；`pasay_bot/handlers/conversation.py:83-86` |
| 四键持久菜单 | `pasay_bot/keyboards.py:320-331` (`reply_keyboard`, is_persistent) |
| Owner=zh / Secretary=en / 群 bi | `pasay_bot/roles.py:59-62,100-116` |
| 快速视图确定性 / Fast Path | `app/services/operations/quick.py`(全)；`app/api/routers/operations.py:582-666`(/quick/*) |
| 单房 digital file | `app/services/operations/quick.py:38` `build_unit_timeline`；`render/cards.py:1375` `unit_timeline_card` |
| Owner Attention 过滤 | `app/services/operations/owner_scope.py:35` `is_owner_actionable`；`app/api/routers/operations.py:219-223` |
| 承诺/催办/升级机制 | `app/services/operations/promises.py:43,113` |
| Repair 主动跟进闭环 | `app/services/operations/repair_flow.py:83` `ensure_evidence_followup`；`app/api/routers/operations.py:280-283` |
| Expense 硬规则 APPROVED≠PAID | `app/services/operations/quick.py:291` `_payable_expense_rows`；`build_quick_tasks`(admin 追加) |
| Expense 去重不凭金额 | `app/services/operations/quick.py:399` `find_similar_paid_expenses` |
| Rent income 状态机 + owner confirm/reverse | `app/api/routers/income.py:144,187`；`app/models/financial.py:11` |
| Rent 幂等 | `app/models/financial.py:20` `uq_incomes_idempotency_key` |
| merchant_id = 0 命中 | 全仓 `grep -i merchant` 空 |
| 模型无 merchant 列 | `app/models/property.py/tenant.py/lease.py/financial.py/operations.py/task.py` |
| Property Channel 缺 | `evidence.py`(仅媒体存档) 非档案文章；无 property↔message 映射迁移 |
| Lifecycle 半成品 | `app/models/property.py:43` `unit_state` VARCHAR；`app/api/routers/units.py:98` `_record_lifecycle` |
| 导入/导出缺 | `grep openpyxl/csv/export/import` 无匹配；`app/api/routers/reports.py` 仅 JSON 聚合 |
| Control Panel 缺 | `app/main.py:60-67` 仅 `/health` DB ping；无面板/指标端 |
| 双任务系统（LEGACY） | `app/models/task.py`(TaskStatus open…scheduled) vs `app/models/operations.py`(operational_tasks)；`app/api/routers/tasks.py` 仍挂载 |
| 旧 unit_status 枚举并存 | `app/models/property.py:21` `UnitStatus(vacant/occupied/maintenance)` 与 `unit_state` 共存 |
| 测试证据面 | `tests/` 26 文件、`pasay-telegram-bot/tests/` 40 文件 |

> 附：Penpot 15 页原始 dump 存于 `.audit/pages/00.md … 14.md`（MCP 读取产物，只读）。

---

## 6. 符合度口径说明
- 每「子项」按产品要求判定；「PASS」= 产品要求已满足，「PARTIAL」= 部分能力具备但缺关键约束，「MISSING」= 无实现，「LEGACY」= 存在历史重复/补丁路径需处置，「CONFLICT」= 有实现但与产品直接矛盾（本次无）。
- 综合 61% = 10 PASS×1.0 + 2 PARTIAL×0.5 + 5 MISSING×0 + 1 LEGACY(不计负分)，共 18 个能力点 → 11/18 ≈ 61%。**此百分比不包含 LEGACY 负分**（重复/补丁需剥除但不扣产品符合度点数，单独列为处置项）。
- A/B/C 判定遵循 PENPOT 14 页「旧代码策略」：只有证据表明阻碍产品实现才判 C；本报告 5 个 C 均为「需新建的能力」而非「推翻旧代码」。

---

*本报告为只读产品符合度审计输出。完成即停，未进入开发。*
