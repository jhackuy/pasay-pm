# Pasay AI Development Workflow Rules — Canonical

rules_version: 2026-08-13.3
canonical_path: /Users/jhackuy/Projects/pasay-pm/AI_WORKFLOW_RULES.md
authority: 唯一权威规则文件。任何 Windows 副本（例如 D:\AI-Review\pasay-pm\AI_WORKFLOW_RULES.md）只作为 mirror/cache，不作为权威。
last_updated: 2026-08-13

---

## 0. 使用方式：Preflight + ACK（全程序化）

### Rules Preflight（任何 LLM 调用之前必须执行）

1. 定位 canonical 规则文件（本文件）。
2. 计算 SHA-256。
3. 与 task envelope 中的 `rules_sha256` 比较。
4. 结果状态：
   - 一致 → `RULES_PREFLIGHT_OK`
   - 不一致 → `BLOCKED_RULES_MISMATCH`（禁止启动 Max/Lily）
   - 缺失 → `BLOCKED_RULES_MISSING`（禁止启动 Max/Lily）

实现：`scripts/wf/wf_ctl.py preflight`。

### Agent ACK（只在同一个实际开发任务的首次响应中出现）

格式（机器可解析，单行）：

```text
RULES_ACK role=MAX sha256=<hash>
RULES_ACK role=LILY sha256=<hash>
```

解析与比对由程序完成：`ACK_VALID` / `ACK_INVALID` / `ACK_MISSING`。

禁止：

* 单独创建规则确认 agent
* 让 LLM 写“我已理解规则”
* 多次催促 Agent 确认规则

规则 hash 是否一致的判断由 Bridge preflight 完成；LLM ACK 只是证明当前 Agent session 使用了指定规则版本，不能替代程序校验。

---

## 1. Task Envelope

每个任务必须包含：

```yaml
task_id: PASAY-XXX
rules_path: <canonical path>
rules_sha256: <sha256>
role: MAX
objective: ...
scope: ...
allowed_paths: [...]
forbidden_paths: [...]
constraints: [...]
forbidden_actions: [...]
acceptance_criteria: [...]
required_context: [...]
```

每个任务结果必须包含：

* status
* files_changed
* tests_run
* tests_passed
* failures
* risks
* unresolved
* diff / commit / artifact reference
* rules_version
* rules_sha256

禁止通过 ChatGPT → Fugui → Lily → Max 逐层重复粘贴整份规则。

---

## 2. 任务隔离

### 2.1 唯一 Task ID

后续所有任务必须拥有唯一 `task_id`。Bridge 必须记录：

* task_id
* baseline HEAD
* baseline git status
* worker/session ID
* allowed_paths
* forbidden_actions
* start_time
* result

### 2.2 Task Worktree（优先）

普通开发任务默认不在 canonical working tree 直接工作：

```text
canonical repo
  → task-specific git worktree（例如 worktrees/<TASK_ID>）
  → worker 只在该 worktree 工作
```

要求：

* 创建任务时自动建立 worktree
* worker 只获得该 task worktree
* canonical repo 不直接修改
* review 前不自动 merge
* review 完成后才决定保留/删除 worktree
* 禁止自动提交到远程
* 禁止部署

如果 Bridge 架构暂时无法完成 worktree 改造，则至少实现 allowed-path enforcement，并在结果标记 `WORKTREE_ISOLATION=PENDING`，但不阻塞任务。

### 2.3 allowed_paths 门禁

执行前：程序记录 baseline（HEAD、tracked modified、staged、untracked）。

执行后：程序获取 tracked modified / staged / untracked 并与 `allowed_paths` 比较。

任何越权路径：立即返回 `BLOCKED_SCOPE_VIOLATION` 并列出具体文件。

恢复原则：

* 禁止无范围 `git reset --hard` 或其他可能删除任务开始前已有修改的操作
* 只恢复当前 task 自己产生的越权变化
* 程序无法安全确定变化归属时：`FAIL CLOSED`，不要猜

### 2.4 Session 隔离

每个 worker 绑定：`task_id + session_id/PID + worktree`。

* 任务结束：worker 退出，session 标记 `CLOSED`
* 后续输出不得写入新的 task
* 发现 orphan worker：程序停止或隔离，并记录
* 不要由 LLM 判断“这个是不是旧任务”

---

## 3. Program First, LLM Last

所有任务优先级固定为：

1. 确定性程序逻辑 / 状态机
2. Shell / Python 脚本 / CLI
3. 数据库查询 / 测试框架 / 日志解析
4. 单个 LLM
5. 多 Agent / 更强 LLM，仅在前面方案无法可靠完成时升级

凡是程序、脚本、规则、exit code、SQL、测试框架能够确定完成的事情，禁止为了方便而调用 LLM。

LLM 主要用于：

* 需求理解
* 架构权衡
* 复杂代码实现
* 未知异常诊断
* UX / 产品语义判断
* 最终语义审核

目标：降低 token、降低延迟、减少重复推理、提高开发速度和确定性。

---

## 4. 角色分工

### ChatGPT（唯一上层决策与审核角色）

负责：产品设计、架构、任务定义、风险边界、验收标准、最终审核、决定是否升级复杂任务。

### Max / Codex（默认开发执行者）

普通任务直接完成：查看必要代码、修改代码、targeted tests、修复失败、regression、输出结构化结果。

普通 bug、普通 feature、重构、API、Bot UX、测试等，默认不经过 Lily。

### Lily / Hermes（不再是每个任务的必经 planner）

仅在以下情况升级介入：

* 长时间自主任务
* 跨模块复杂任务
* 多阶段任务
* 多 Agent 编排
* Max 连续失败
* 需要持续恢复 / 重试
* 全仓库审计
* 需要 supervisor 才能明显提高成功率

原则：

* Normal Path：ChatGPT → Max
* Escalation Path：ChatGPT → Lily → Max

### Fugui（Windows 控制节点，退出常规推理链）

负责：Windows 控制节点、Bridge、Windows 实机验证、Owner Telegram UX 测试、自动测试协调、日志/状态收集、结果回传、必要时通知 Owner 人工验收。

不要重新分析 ChatGPT 已经明确的需求，也不要为了转发任务再次长篇总结。

---

## 5. 机器间禁止长篇自然语言交接

Agent 之间尽量传结构化数据，不写互相报告。

任务最少包含：task_id、objective、scope、constraints、forbidden_actions、acceptance_criteria、required_context。

结果最少包含：status、files_changed、tests_run、tests_passed、failures、risks、unresolved、diff/commit/artifact reference。

不要让每一层重新解释一遍相同背景。

---

## 6. 唯一事实源落盘

项目长期信息不能依赖聊天上下文。逐步建立并维护 `.ai-control/`：

* PROJECT.md
* ARCHITECTURE.md
* RULES.md（或本文件）
* CURRENT_STATE.json
* TASKS/
* RESULTS/

原则：

* 长期知识放文件
* 当前状态放结构化数据
* LLM Context 只读取当前任务真正需要的信息

不要每次重新读取和解释整个项目历史。

---

## 7. 测试分级

* Level 1：修改相关 targeted tests
* Level 2：相关模块 regression
* Level 3：全量测试

普通开发优先 Level 1 → Level 2。只有 Gate、重要提交、merge、release 或高风险改动时，才默认执行 Level 3。

不要每次小修改都跑全量测试。

---

## 8. 日志必须先程序处理

禁止把大量原始日志直接喂给 LLM。

程序先提取：FAILED、ERROR、traceback、affected tests、exit code、relevant tail/context；能生成 JSON 就生成 JSON。

LLM 负责分析错误原因，不负责人工从几千行日志里搜索错误。

---

## 9. 确定性判断必须由程序完成

以下类型默认禁止询问 LLM：

* pytest 是否通过 → exit code
* service 是否健康 → health check
* Git 是否 clean → git status
* migration 是否一致 → migration/version query
* 数据是否存在 → SQL
* capability 是否允许 → schema/rules
* 任务状态 → state machine
* 文件是否越界 → path/risk rules

能计算就计算。能查询就查询。能验证就验证。不要“问模型觉得是否通过”。

---

## 10. 人工测试规则

绝大多数测试由程序、Max、Fugui 自动完成。只有以下类型才通知 Owner：

* 真实 Telegram Owner UX
* 必须人眼判断的界面体验
* 授权
* 现场操作
* 产品决策
* 自动化成本明显高于人工几十秒即可完成的测试

需要 Owner 测试时，由 Fugui 主动通知，通知必须极简（打开 Bot → 点击 → 检查 XXX → 回复“正常 / 异常截图”）。不要让 Owner 跟完整开发流程。

---

## 11. 失败后才升级智能

默认最低成本路径：

```text
ChatGPT → Max → automated tests → result → ChatGPT review
```

如果 Max 连续失败、出现架构冲突、范围扩大或任务明显复杂：

```text
→ NEEDS_SUPERVISOR → Lily 介入
```

禁止所有任务一开始就同时启动 Fugui + Lily + Max。

---

## 12. 记录真实成本与效果

每个任务逐步记录：

* ChatGPT token
* Max token
* Lily token
* Fugui token
* 总耗时
* 自动重试次数
* 测试失败轮数
* 人工介入次数
* 最终成功/失败
* 是否回滚

以后根据真实数据决定：哪些任务直接 Max、哪些任务值得 Lily、哪些步骤应该改成脚本。

---

## 13. 程序化路由、升级阈值与成本控制（WF-003）

### 13.1 Task Router（默认不经过 Lily）

每个任务至少包含：task_id、task_type、risk_level、objective、allowed_paths、acceptance_criteria、requires_human_test、requires_supervisor、max_retry、test_level。

程序化路由结果由程序产生：

* `PROGRAMMATIC`：脚本/CLI/SQL/测试可完成 → 不启动 LLM
* `MAX`：普通代码修改默认只启动 Max
* `LILY`：仅当以下任一条件成立（禁止“任务比较复杂”这类模糊理由）：
  * requires_supervisor=true
  * 跨多个高风险模块且规则明确要求
  * Max 达到 max_retry
  * Max 返回 NEEDS_SUPERVISOR
  * 出现无法自动解决的架构冲突
  * 长时间/多阶段自治任务明确要求 supervisor

### 13.2 Single Max Session Per Task + Retry Limit

* 一个 task 默认只允许一个 active Max session。
* 默认 `max_retry=2`：第一次失败允许基于失败证据修复；第二次仍失败 → `NEEDS_SUPERVISOR`，停止烧 Token，之后才允许 Lily 介入。
* 禁止：Max 无限重试、失败一次立刻启动 Lily、同时启动 Max + Lily 重复分析。

### 13.3 Escalation Only On Evidence

升级必须有证据：失败轮数、exit code、测试失败清单、架构冲突记录。LLM 不得自行生成 task status。

### 13.4 Structured Logs（日志压缩程序化）

* 原始日志保存到磁盘（`.ai-control/results/<task_id>/logs/raw.log`）。
* 给 LLM 的内容只包含：command、exit_code、failed_test_names、ERROR/FAILED、traceback 相关片段、最后相关行、affected files/modules，并设置最大长度。
* 日志仍过大 → 进一步截断并提供 raw_log_path，LLM 按需读取指定片段。
* 原则：“日志存在磁盘，不存在 Prompt 里。”

### 13.5 Test Levels（程序化分级）

* L1 = targeted tests；L2 = module regression；L3 = full suite。
* 普通修改默认 L1 → PASS → L2。
* 仅以下情况自动进入 L3：merge/release gate、migration、identity/auth/RBAC、financial write path、shared core infrastructure、明确指定 full regression、L2 结果显示跨模块风险。
* pytest 是否通过由 exit code + structured result 决定，不让 LLM 判断。

### 13.6 Human Test Minimalism

* requires_human_test=false → 禁止通知 Owner。
* true → 只生成最小化操作说明（固定格式：需要你完成 1 个测试 → 步骤 → 完成后只回复 正常 / 异常截图）。
* 测试完成后记录 human_test_result=PASS/FAIL，然后自动恢复机器工作流。

### 13.7 Task Lock（去重与并发保护）

* 同 task_id 已 RUNNING 时再次 dispatch → `BLOCKED_DUPLICATE_TASK`，不启动第二个 worker。
* 不同 task 可并发，但必须不同 worktree、不同 session、不同 task_id；禁止共享可写工作目录。

### 13.8 Metrics（真实成本记录）

* 每个任务自动记录到 `.ai-control/results/<task_id>/metrics.json`：task_id、route、fugui_llm_calls、max_sessions、lily_sessions、max_attempts、start/end_time、duration_seconds、human_interventions、test_runs、test_failures、result。
* provider 能取得 token usage 才记录 input/output/total_tokens；否则记录 UNKNOWN。禁止猜测 token 数量。

### 13.9 状态机

统一状态：CREATED → PREFLIGHT → RUNNING → TESTING → HUMAN_TEST_REQUIRED → REVIEW_READY → DONE；异常状态 BLOCKED_RULES_MISMATCH / BLOCKED_SCOPE_VIOLATION / BLOCKED_DUPLICATE_TASK / NEEDS_SUPERVISOR / FAILED。状态转换由程序决定，非法跳转 → BLOCKED_ILLEGAL_TRANSITION。

---

## 14. 产品 UX 最高法则

### 最高原则

产品是给人用的，不是让人学的。用户只需要表达意图、提供新事实、完成必要决策；系统负责其他事情。

### 五条 UX 最高法则

1. Don't make me learn — 不得要求用户学习系统结构
2. Don't make me repeat — 系统已知道的数据不得再次要求输入
3. Don't make me manage — 用户不得承担 workflow 推进工作
4. Don't make me wait — 操作必须立即反馈
5. Don't bother me unless necessary — AI 能自己处理的不要打扰人

### 三种主要入口

* 系统主动把事项送过来（消息下直接带操作按钮）
* 用户自然语言表达（直接回答/执行）
* 用户直接发送资料（AI 自动识别上下文，不得要求用户先进入菜单录资料）

### Telegram UX 架构

* 主工作区：Telegram Chat
* 主要操作：Inline Action Buttons
* 兜底导航：Persistent Reply Keyboard（🏠 房源 | ✅ 待办 / 💰 财务 | ☰ 更多）
* 不得设计成 菜单 → 子菜单 → 子菜单 → 表单
* 复杂信息未来进入 Telegram Mini App；Chat First, Mini App Second

### 其他关键规则

* 3-Step Review：一次业务连续要求用户超过 3 个动作必须重新设计；高频任务人工操作 ≤ 1
* Zero Re-entry：数据库已有数据不得重复要求输入
* Action-at-source：操作按钮必须跟在事件消息下面，禁止“请前往待办中心处理”
* Human Language Only：界面禁止直接显示 enum/API 术语，转换成人话
* Role-aware UX：Owner 中文看结论/风险/金额/决定；Secretary 英文看下一动作/截止时间/上传证据；Tenant 看服务状态和简单选择
* AI Autonomy：默认尽量落到 L0–L2（自动完成/通知结果/一键确认）；真正商业判断 L3 人决定
* Risk-Based Friction：低风险一次完成，中风险按钮+audit，高风险明确二次确认；不得所有操作统一二次确认
* Context-aware Conversation：理解当前消息、回复关系、最近业务事件、当前用户/房源/租客/工作流
* Error Recovery：AI 猜错不得让用户重新开始，提供最小选择（Undo / Correct / Re-link / Reverse / Retry）
* Next Action Owner：所有运营事件必须能回答“现在轮到谁”，内部有 next_actor / next_action / deadline / severity / requires_human_decision
* Attention Queue：待办数量 = 当前这个人真正需要处理的事项数量
* Notification Budget：正常事件默认静默、完成事件汇总、需要决定即时通知、重大异常即时升级
* Message Mutation：操作后优先更新原消息，不产生大量“成功/已处理”垃圾消息
* 数据真实性：FACT / INFERENCE / RECOMMENDATION 必须区分；推断不得当事实写入核心账务

---

## 15. 安全底线（本任务与所有后续任务）

禁止：

* Pasay 新功能开发（除非任务明确指定）
* 生产数据库写入 / 生产 Telegram 数据写入
* 部署 / push / merge / 自动 commit
* 修改业务数据
* 删除任务开始前已有的未提交工作
* 无范围限制的 git reset
* 让旧 Agent session 持续工作

允许：

* AI workflow / Bridge / runner / task schema / 规则文件必要修改
* 测试
* 创建临时 worktree
* 本地规则同步
* deterministic validation

Git 保持可回滚。开发环境优先开发速度 > 流程完整度，但以上安全底线不放松。

---

## 16. 最低流程开发模式（Minimal Process Dev Mode）

生效：2026-08-13。普通产品切片默认按本模式执行；本模式覆盖 §7/§13.5 的默认测试分级与 §4 的默认路由中更重的默认流程。

### 16.1 测试范围：普通 Bot 小功能禁止默认全量

普通 Bot 小功能只跑：

1. 本次新增测试
2. 与当前改动直接相关的 regression
3. 必要 smoke

禁止默认运行：Bot 全量 suite、Backend 全量 suite、完整 Gate、Windows 重复测试、历史 Gate 重跑。

例外（才允许扩大范围）：修改 Bot 核心路由/共享公共模块，或 targeted tests 出现异常。

### 16.2 完成后自动继续，禁止停在“等待下一张任务卡”

只要下一产品功能已经明确：PASS → commit → 自动继续下一产品切片。

只有以下情况才停：

* USER_ACTION_REQUIRED
* MANUAL_APPROVAL_REQUIRED
* BLOCKED / FAILED
* TIMEOUT
* 下一功能优先级确实不明确（NEXT_SLICE_PENDING）

### 16.3 正常开发链固定

```text
ChatGPT → Fugui/Bridge 传输 → Lily → Max 开发 → targeted tests → commit → 下一任务
```

* Bridge 只负责传输。
* Windows/Fugui 不重复测试 Mac 已经 PASS 的内容。
* 普通开发不同步 Windows、不跑完整 Gate。
* 不要重新执行历史 Gate、全仓库审计、全量测试或流程检查。
