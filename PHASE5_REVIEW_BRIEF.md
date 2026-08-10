# Phase 5 — 二次安全/代码 Review（给 Codex Max, 切换角色 = Senior Code Reviewer + Security Reviewer）

你现在**不是**实现者，而是独立审查者，审查你在 Phase 2 完整实现的 `pasay-telegram-bot/` 原生 Bot 代码。
工作目录 `~/Documents/Codex/pasay-pm`。目标是找出会导致**误记/漏记/重复入账/越权/注入/泄漏**的问题。
审查对象 = `pasay-telegram-bot/` 的 `git diff`（相对 main 的新增代码）。**本轮先只读审查，输出 REVIEW_FINDINGS.md，不要改代码**；等你我确认后再修 CRITICAL/HIGH。

## 背景（供你对照）
- Native Bot 通过 Pasay FakeAPI（httpx）写，**从不直接连 PostgreSQL**；收租 = `POST /incomes{pending}` → `POST /incomes/{id}/confirm` → reverse(admin)；ID 在 `state/idempotency.py`（in_flight/done/failed）+ nonce→callback_data；超时用 `GET /incomes/{id}` 对账。
- RBAC：`roles.py`（OWNER 全权 / SECRETARY 只登记不能 confirm）+ 后端 API key 角色（agent/manager/admin）作为最终 enforcement。
- HTML parse_mode；文本全走 `render/html.py:.escape()`；金额 `H.money()`（Decimal）。
- callback_data `v1:<action>:<entity>:<ref>:<nonce>:<ts>` ≤64B；15 分钟过期。

## 排查清单（任务 §23 要求，逐项给结论：CRITICAL/HIGH/MEDIUM/LOW + 是否需修）

1. 重复入账（双击 confirm）
2. 重复 callback / callback replay（旧按钮、重放）
3. Telegram retry（网络层重试导致重复）
4. API timeout（确认返回超时怎么办——必须**不产生第二笔**、不误报"未修改"）
5. DB transaction 一致性（Native Bot 无 DB 写，但确认 API 调用的原子性、pending→confirm 断链恢复）
6. 并发 / race condition（两个并发 confirm 同一 income；`insert_idempotency_if_absent` 是否真原子；SQLite 并发写）
7. permission bypass（手工构造 callback_data 能否让 SECRETARY/无权限者 confirm/reverse；后端 403 是否真兜底）
8. float 金额（所有金额是否 Decimal/字符串，`api_client.py` 传值是否 str）
9. HTML injection（未转义的属性/地址/租客名；`can't parse entities`）
10. callback data 泄露（是否把租客电话/敏感 PII 放进 callback_data）
11. stale button（过了 15 分钟 / 换账期 / 换 period 的旧确认卡点击后会不会入账或误报）
12. audit log 缺失（确认/冲销是否都走后端 confirm/reverse → 后端 audit；Bot 是否漏了某条写路径不走 API）
13. reverse 异常（reverse 失败/超时/重复的处理）
14. pagination bug（溢出/越界/负数页/超大页）
15. 状态机漏洞（conversation state 转移遗漏、非法 state 组合、cancel/expire 竞态）
16. 直接 DB 写操作（确认没有任何绕过 API 的写）
17. LLM 幻觉导致财务数据变化（Native Bot 是否完全不依赖 LLM 决定金额/状态）
18. i18n key 缺失（en/zh 缺翻译导致 KeyError/Fallback 还是裸 key）
19. 日志/错误处理（用户端是否出现 traceback / SQL error / JSON error；错误编号是否存在）

## 额外自查（容易被忽略）
- 未知 telegram_user_id 点击 confirm 会不会被拒（`has_permission(None)`）
- group 场景：`(chat_id, user_id)` 主键是否防串话；群内非 OWNER/SECRETARY 能否触发写
- `api_client.py` 的 timeout/`PasayApiTimeoutError` 判定是否可靠（真超时 vs 500 区分）
- `config.py` secret 是否被打印/进日志 / `.env.example` 是否含占位符而非真值
- `main.py --dry-run` / start-native-bot.sh 的 token 是否可能漏进日志
- 测试是否真实覆盖（不是空断言）；`test_backend_timeout_after_write` 是否真的模拟"后端已写入但响应超时"

## 输出
产出 `REVIEW_FINDINGS.md`（放 pasay-telegram-bot/ 或仓库根），格式：
```
## REVIEW_FINDINGS
| # | Level | Area | 位置(文件:行) | 问题 | 建议 |
```
按 CRITICAL→LOW 排序。再给一段**结论**：哪些必须修（CRITICAL/HIGH）、哪些可不修（MEDIUM/LOW）及理由。
最后输出简短总结（发现数/分级/是否建议修复后再回归）。

只读审查，不修改代码、不 commit、不部署。
