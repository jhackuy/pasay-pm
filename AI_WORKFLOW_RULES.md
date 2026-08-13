# Pasay AI Development Workflow Rules — Canonical

rules_version: 2026-08-13.1
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

## 13. 产品 UX 最高法则

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

## 14. 安全底线（本任务与所有后续任务）

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
