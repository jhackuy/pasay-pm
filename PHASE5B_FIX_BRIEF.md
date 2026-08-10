# Phase 5b — 修复二次 Review 发现（给 Codex Max, 回到实现者角色）

基于你在 `REVIEW_FINDINGS.md` 的结论，现在**修复**以下问题。修复范围与优先级：

## 必须修（CRITICAL + HIGH，上线硬门槛）
- **F1 CRITICAL（create 超时后重试重复入账）**：`POST /incomes` 已写入但响应超时 → 同卡重试产生第二笔。用你建议的方案 A 实现：create 超时后按 `(lease_id, received_date, amount, method)` 做 GET 对账，命中则复用并 settle，**绝不再次 create**；换新卡（新 nonce）重录同一单元同账期也要避免同一 (lease,period) 重复。并把 `guard.fail(key, resource=<income_id>)` 的 resource 在对账命中时补记。**必须新增回归测试 `test_create_timeout_after_write_no_duplicate`**；同时修正现有 `test_backend_timeout_after_write` 使其覆盖 create 路径（此前只覆盖 confirm）。
- **F2 HIGH（reverse 不可用 / 后端兜底降级）**：按操作切换 key。实现：`api_client` 持两个 key（PASSAY_API_KEY=manager 做 create/confirm；PASSAY_ADMIN_API_KEY=admin 做 reverse）。OWNER 触发 reverse 时用 admin client；create/confirm 始终用 manager client。若无 admin key 配置则 reverse 按钮对 OWNER 也不显示，并记录日志说明未配置。**必须**让 `pasay_admin_api_key` 真正被使用，并新增测试断言 reverse 走 admin key / create+confirm 走 manager key。

## 建议修（MEDIUM，影响正确入账闭环与权限边界）
- **F3 MEDIUM（in_flight 崩溃锁死 7 天）**：in_flight 加短老化（如 120s 后视为 failed 允许重试）；启动时把遗留 in_flight 批量转 failed。补「写后进程中断→重启→同卡重试不产生第二笔」测试。
- **F4 MEDIUM（读路径无权限门禁）**：/properties /finance /overdue /rent 及导航/菜单，按 `PERMISSION_READ` 校验：OWNER/SECRETARY 放行，未知/群内陌生人拒绝。补未知用户调 /finance 被拒测试。
- **F5 MEDIUM（SECRETARY 登记 pending 无确认入口）**：给 OWNER 提供 pending 待确认列表（如 /pending 命令，或逾期/财务卡上对 pending 收入提供 OWNER 可点的 `cnf:inc <id>`），OWNER 可一键确认 SECRETARY 录入的 pending。补 OWNER 通过 /pending 确认 SECRETARY 录入的测试。
- **F6 MEDIUM（群聊烧 nonce 锁死）**：把会话归属校验（conv nonce 匹配）与权限校验移到 `guard.acquire` **之前**，避免先建 in_flight 再被拒导致锁死；或 idempotency key 加 chat/user 维度。补「同群他人点击不锁死本人卡」测试。

## 顺手修（LOW，低成本）
- **F7**：reverse 超时对账后若仍 confirmed，toast 显示「未冲销，请重试」而非「已处理」。
- **F8**：`H.truncate` 截断后做标签闭合/实体安全校验（截到合法边界），或超长时改纯文本 fallback；补长列表+含 `&` 名称测试。
- **F9**：`store.get_idempotency` 的 `json.loads` 包 try。
- **F11**：后端 `app/api/routers/income.py` 的 confirm/reverse audit `old_value=serialize_row(obj)` 改为在变更前取值（`old_value=old`），修复审计无法还原变更前状态。# 注意：这会触碰后端 `app/`，是**安全的小修复**（一行），请只改这一处并跑后端测试确认不破坏 102 项。# F10（金额静默归零）与日志/错误处理保持现状，本轮不处理。

## 硬约束
1. 不做 DB schema migration（除非你判断 F1 必须靠后端唯一约束才能真正根治——若是，先把方案讲明的 migration 写好但**不执行不应用**，并说明；本轮优先 bot 层 + 新增回归测试解决）。
2. 不改现有后端 102 个测试的通过状态；新增测试只增不减。
3. 修改后：`env -u PYTHONPATH .venv/bin/python -m pytest tests`（bot）全绿 + `pytest tests`（后端）仍 102 全绿。
4. 不要 commit、不要部署、不要注册 launchd。
5. 更新 `REVIEW_FINDINGS.md`：在每条已修复的问题上标注 🔧 已修复 + 提交备注，或新增一个「Phase 5b 修复记录」小节列出每一项的处理与验证。

完成后输出简短总结：每项 (F1-F9,F11) 的处理方式、新增测试、最终两套 pytest 结果。
