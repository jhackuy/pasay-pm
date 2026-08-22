---
alwaysApply: true
---

# Pasay SOLO — Hard Safety Bans ONLY

本文件是 alwaysApply 硬安全禁令层。**只包含不可逾越的硬禁令**，不包含流程、规范、建议或任何非禁令内容。

违反以下任何一条 = 立即 FAIL CLOSED：

## 1. Git Safety
- 禁止 force push / force-with-lease / 改写共享历史
- 禁止 overwrite remote-only commits / 删除共享 remote 分支
- 禁止绕过 PR 直接修改 authority 或 base-branch 业务代码
- 禁止 auto-merge

## 2. Authority & Deployment
- 禁止 Merge PR（SOLO 不 merge；Owner 最终 merge）
- 禁止 Production deploy（SOLO 不部署）
- 禁止写入 Production secrets / credentials / 私钥到仓库

## 3. Business & Product Integrity
- 禁止自行改变产品方向 / 核心业务模型
- 禁止自行重定义 Owner / Secretary / Tenant 权限边界或角色语义
- 禁止自行推翻冻结架构（`ARCHITECTURE_FROZEN=YES`）
- 禁止删除现有已确认业务能力
- 禁止削弱已确认业务事实或核心产品规则
- 禁止 Operation ↔ Task 真值反转：Operation 永远是真值，Task 状态永远不能反向决定业务真值

## 4. Test & Validation Integrity
- 禁止删除、skip、xfail 真实失败测试来制造 PASS
- 禁止修改已确认业务事实、弱化约束或改变冻结行为来换取 green
