# Pasay AI Property Operations — V1.3 开发控制规则

## 1. 当前环境定义

当前 Mac mini 上的 Pasay 项目是：

**纯开发环境，不是生产环境。**

开发阶段第一优先级：

**开发速度 > 环境流程完整度。**

禁止继续套用不必要的生产级控制，包括但不限于：

* 过度审批
* 多层人工确认
* 不必要的环境隔离
* 为理论风险建立复杂 Gate
* 每个小改动都建立重型验证流程
* 因为“未来可能生产使用”而提前建设完整生产基础设施

仍保留必要底线：

* 不误操作真实业务数据库
* 不破坏当前已验证财务幂等性
* 不绕过 RBAC
* 不做不可恢复的数据破坏
* 不把测试数据混入真实业务数据
* Git 保持可回滚

真正生产部署将在云端进行，届时单独进行 Production Hardening。

---

## 2. Agent 分工

### Windows Codex

角色：

**Development Orchestrator**

负责：

* 接收 ChatGPT 产品设计和开发指令
* 判断任务拆分
* 将计划交给 Mac Lily
* 检查 Lily / Max 回传结果
* 汇总进度
* 遇到真正需要产品决策的问题再升级给 ChatGPT / Owner

不要要求 Owner 做 Agent 项目管理。

---

### Lily（Mac）

角色：

**Planner / Reviewer / Executor Coordinator**

职责：

* 阅读产品规格
* 审查当前仓库实际状态
* 根据真实代码调整实施计划
* 把代码工作交给 Max
* 监督测试
* 检查 Max 是否偏离产品设计
* 完成 UX 验收
* 汇总证据

Lily 不应为了流程本身制造额外流程。

---

### Max

角色：

**Primary Coding Agent**

负责：

* 阅读实际代码
* 实现功能
* 修改数据库/API/Bot/UI
* 添加和修改测试
* 运行测试
* 修复回归
* 输出实际变更证据

原则：

**能直接修改代码解决的问题，不要只写分析报告。**

---

## 3. 产品最高原则

所有功能设计和代码实现必须服从：

> **产品是给人用的，不是让人学的。**

用户不需要知道：

* 功能在哪个菜单
* 数据库内部状态
* 模块结构
* workflow 名称
* API 名称
* income / expense / settlement 等内部模型
* Bot command

用户只需要：

**表达意图、提供新事实、完成必要决策。**

系统负责其他事情。

---

## 4. 五条 UX 最高法则

### ① Don't make me learn

不得要求用户学习系统结构。

### ② Don't make me repeat

系统已经知道的数据不得再次要求用户输入。

### ③ Don't make me manage

用户不得承担 workflow 推进工作。

### ④ Don't make me wait

操作必须立即反馈。

### ⑤ Don't bother me unless necessary

AI 可以自己处理的事情不要打扰人。

---

## 5. 三种主要入口

用户完成业务只应该通过：

### A. 系统主动把事情送过来

例：

支出待审批 → 消息下直接显示：

* 批准
* 拒绝
* 查看凭证

### B. 用户自然语言表达

例：

“这个月谁还没交租？”

直接回答。

### C. 用户直接发送资料

包括：

* 转账截图
* 发票
* 租约
* 照片
* 聊天截图

AI 自动识别上下文。

不得第一反应要求用户自己进入菜单录资料。

---

## 6. Telegram UX 架构

### 主工作区

Telegram Chat。

### 主要操作

Inline Action Buttons。

### 兜底导航

Persistent Reply Keyboard：

| 🏠 房源 | ✅ 待办 |
| ----- | ---- |
| 💰 财务 | ☰ 更多 |

菜单只是 fallback。

不得将关键流程设计成：

菜单 → 子菜单 → 子菜单 → 表单。

### 复杂信息

未来进入 Telegram Mini App。

Chat First。

Mini App Second。

---

## 7. 3-Step Review Rule

如果一次业务连续要求用户做超过 3 个动作：

**必须重新设计。**

审查：

* 哪一步系统已经知道？
* 哪一步 AI 可以自动完成？
* 哪一步可以使用默认值？
* 哪一步可以通过上下文推断？
* 哪一步实际上没有必要？

目标：

高频任务人工操作 ≤ 1。

---

## 8. Zero Re-entry

数据库已有数据不得重复要求输入。

例如收租时：

系统已经知道：

* property
* unit
* tenant
* lease
* billing period
* expected amount
* due date

用户不应重新填写。

---

## 9. Action-at-source

任何需要用户处理的事情：

**操作按钮必须跟在事件消息下面。**

禁止：

“请前往待办中心处理”。

例：

支出审批：

[批准] [拒绝]

租金：

[已收款] [催租]

维修：

[确认安排] [修改]

续约：

[接受建议] [保持原价] [其他]

---

## 10. Human Language Only

普通用户界面禁止直接显示：

* APPROVAL_PENDING
* RENT_DUE
* PAYMENT_PENDING
* expense_id
* income_id
* settlement_id
* 数据库 enum
* API terminology

转换成人话。

---

## 11. Role-aware UX

同一个事件不能给不同角色显示相同界面。

### Owner

中文。

重点：

* 结论
* 风险
* 金额
* 是否需要决定

### Secretary

英文。

重点：

* 下一动作
* 截止时间
* 联系对象
* 上传证据
* 完成按钮

### Tenant

服务状态和简单选择。

后台同一个 event。

前台按接收者重新生成。

---

## 12. AI Autonomy

### L0

AI 自动完成，无需通知。

### L1

AI 自动完成，只通知结果。

### L2

AI 准备完，人一键确认。

### L3

真正商业判断，人决定。

设计默认尽量落到：

**L0–L2。**

---

## 13. Risk-Based Friction

少点击 ≠ 无安全控制。

低风险：

一次完成。

中风险：

一次按钮 + audit。

高风险：

明确二次确认。

不得所有操作统一二次确认。

---

## 14. Context-aware Conversation

AI 必须理解：

* 当前消息
* 回复关系
* 最近业务事件
* 当前用户
* 房源
* 租客
* 当前 workflow

应支持：

“这个批准。”

“刚才那个取消。”

“不是这个房子。”

“昨天收到的。”

不得逼用户重新完整描述。

---

## 15. Error Recovery

AI 猜错不得让用户重新开始。

提供最小选择：

“我找到两个可能对应的租约。”

[1608] [1708]

支持：

* Undo
* Correct
* Re-link
* Reverse
* Retry

---

## 16. Next Action Owner

所有运营事件必须能够回答：

**现在轮到谁？**

内部至少拥有：

* next_actor
* next_action
* deadline
* severity
* requires_human_decision

next_actor 可以是：

* AI
* OWNER
* SECRETARY
* TENANT
* AGENT
* VENDOR
* SYSTEM
* NONE

---

## 17. Attention Queue

用户看到的“待办数量”：

不是系统所有任务数量。

而是：

**当前这个人真正需要处理的事项数量。**

Owner 不应该看到 AI 和 Secretary 已经能够处理的事项。

---

## 18. Notification Budget

AI 不得因为“有能力通知”就发送消息。

原则：

正常事件 → 默认静默。

完成事件 → 汇总。

需要决定 → 即时通知。

重大异常 → 即时升级。

---

## 19. Message Mutation

用户执行操作以后：

优先更新原消息。

不要产生大量：

“成功”
“已处理”
“操作完成”

垃圾消息。

---

## 20. 数据真实性

AI 可以：

* 推断
* 建议
* 总结
* 分类

但不能把推断当事实写入核心账务。

所有重要结果必须区分：

* FACT
* INFERENCE
* RECOMMENDATION

财务事实仍以数据库和证据为准。

---

## 21. 开发优先级

当前阶段不要新增大量后台功能。

优先把已经存在的功能做好 UX：

1. Action Cards
2. Persistent Keyboard
3. Attention Queue
4. Role-aware UI
5. Natural-language execution
6. AI automation
7. Immediate feedback
8. Zero re-entry
9. Context handling
10. Error recovery

---

## 22. 开发验收方式

不要只验收：

“API pass”。

必须验收真实用户任务。

例如：

### 收租

用户收到/上传付款信息。

目标：

≤1 次确认完成入账。

### 支出审批

消息出现。

目标：

1 click 完成。

### 维修

用户直接描述问题。

目标：

无需找菜单自动建 workflow。

### 查询

用户直接问：

“哪个房子最久没租出去？”

目标：

直接回答。

---

## 23. 开发效率原则

当前是开发环境。

Lily / Max：

* 可以快速迭代
* 可以重构开发库
* 可以增加 migration
* 可以重建测试库
* 可以运行完整测试
* 可以调整 Bot UI
* 可以改 API
* 可以调整模型

不要因为未来生产场景拖慢当前产品验证。

每一个开发 Slice 应该尽可能做到：

**设计 → 实现 → 测试 → TG 实机体验 → 修正**

快速闭环。
