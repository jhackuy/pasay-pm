# Phase 5c — 最终 code/security review（给 Codex Max, 再次切换 = Senior Reviewer）

上一轮你的 `REVIEW_FINDINGS.md` 已记录 F1-F9/F11 全部修复。现在做**最终确认式审查**，确认修复本身正确、没引入新问题，且没有任何 CRITICAL/HIGH 残留。

工作目录 `~/Documents/Codex/pasay-pm`。只看当前代码 + 完整 `git diff`（含 `pasay-telegram-bot/` 新增与 `app/api/routers/income.py` 的 F11 改动）。只读，不改代码、不 commit、不部署。

## 审查重点（针对你上一轮修复后的 diff）
1. **F1（CREATE 超时对账 + `find_income` 复用）**：重读 `_confirm_rent_entry` + `api_client.find_income`，确认：
   - create 前对账只在「无已知 resource」时才走；
   - 对账命中即复用+settle+补记 resource，不二次 create；
   - 换新卡/新 nonce 重录同账期也不重复；
   - `test_create_timeout_after_write_no_duplicate` 是真实断言（复现原 bug 场景）。
2. **F2（双 key：manager 写 / admin 冲销）**：确认 reverse 确实走 admin client、create/confirm 走 manager client；无 admin key 时不显示冲销按钮 + 手工回调拒绝；对应测试真实。
3. **F3（in_flight 120s 老化 + 启动恢复）**：确认老化逻辑在 `acquire`/`store` 生效、启动批量转 failed 不误伤 done；重启恢复测试真实。
4. **F4（读权限门禁）**：确认 `/properties /finance /overdue /rent`、菜单/导航/NL 路由都按 `has_read_permission` 门禁；未知用户被拒；OWNER/SECRETARY 放行。
5. **F5（/pending 命令，OWNER 确认 SECRETARY 的 pending）**：确认入口存在、OWNER 可 `cnf:inc`、SECRETARY 无按钮。
6. **F6（conv 归属校验前移）**：确认校验在 `guard.acquire` 之前，群内他人点击不锁 nonce。
7. **F7-F9 + F11**：toast 歧义、truncate 标签闭合、json.loads 容错、后端 audit old_value——确认实现正确且测试真实。
8. **回归确认**：`env -u PYTHONPATH .venv/bin/python -m pytest tests`（bot，应 99 全绿）+ `.venv/bin/python -m pytest tests`（后端，应 102 全绿）。
9. **无新 CRITICAL/HIGH**：通看当前 diff 是否引入新的会导致 重复入账/越权/注入/金额错误 的问题。

## 输出
- 若全部通过且无 CRITICAL/HIGH：在 `REVIEW_FINDINGS.md` 末尾新增「Phase 5c 最终确认」小节，列「通过项 + 结论（无残留 CRITICAL/HIGH）」，并给出简短总结。
- 若发现新 CRITICAL/HIGH：列出并给修复建议；若只是 MEDIUM/LOW 记录而不阻塞。

只读，不改代码、不 commit、不部署。
