# Phase G — 实机生产最终 Review（PRODUCTION_REVIEW）

- 时间：2026-08-10 ~20:30–20:40（Asia/Manila）
- 审查对象：/opt/pasay-pm 实机部署（launchd ai.pasay.telegram-bot / ai.pasay.api / ai.pasay.postgres）
- 审查方式：只读为主；F11 验证经生产 API 造 #22（₱3.33）→ confirm → reverse，已完整清理。
- git diff：feature/telegram-ui-v2（含未提交工作区：i18n 撤销 + F12 edit 幂等）

## 逐项核查

### 1. token ownership — PASS（当前状态） / ⚠️ 重启后有冲突风险（阻塞项）
- @pasayhousebot（id=8777030651）native bot 为唯一活跃 consumer：bot 日志 getMe OK；PID 87907 与 Telegram（149.154.166.110:443）保持 2 条 ESTABLISHED polling 连接；日志无 getUpdates 409 Conflict。
- native token 仅存在于 `/opt/pasay-pm/pasay-telegram-bot/.env`（600）与休眠的 Hermes profile `/Users/jhackuy/.hermes/profiles/pasay-property/.env`（同一 token）。
- 主 @zhushoumacbot（id=8820506233）原封未动：用 Hermes `TELEGRAM_BOT_TOKEN` 实调 getMe 返回 zhushoumacbot；Hermes app（launchd ai.hermes.app, PID 52221）与 gateway（PID 50213, telegram platform connected）均在运行。
- ⚠️ 风险：`/Library/LaunchDaemons/ai.hermes.property-gateway.plist`（RunAtLoad+KeepAlive，HERMES_HOME=pasay-property profile）**当前未加载**（launchctl 找不到服务、PID 70760 已死、19:56 有 .clean_shutdown），但**不在 launchctl print-disabled 列表中**。下次重启 launchd 会自动加载它 → 第二个 getUpdates consumer 与 native bot 冲突（Hermes 日志明示 "persisting gateway_state=running so container_boot auto-starts on the next boot"）。

### 2. launchd / service lifecycle — PASS
- ai.pasay.telegram-bot：/Library/LaunchDaemons，RunAtLoad=true，KeepAlive=true，ThrottleInterval=30，ExitTimeOut=25，UserName=jhackuy，StandardOut/ErrPath 齐全。launchctl print：state=running，pid=87907，runs=3（已重启 2 次），last exit code=0。
- ai.pasay.api：同样配置，pid=87233，runs=2，自 20:25 运行。
- 启动脚本 fail-closed：API 先 pg_isready 再 alembic upgrade head 才起 uvicorn；bot 先 getMe self-check（--dry-run）再 polling。
- 历史：api.err.log 可见早期 plist 曾指向 git 仓库路径（Operation not permitted），现 plist 已改 /opt/pasay-pm/bin/start-native-api.sh，已解决。

### 3. SQLite state 持久化 — PASS
- `/opt/pasay-pm/pasay-telegram-bot/state/bot_state.db` 正常读写：conversations + idempotency_keys 两表齐全，WAL 活跃，PID 87907 持有 fd，mtime 20:32。
- #20 于 20:17 写入的 idempotency keys 在 bot 两次重启（runs=3）后仍完整保留。

### 4. restart 后 in_flight 恢复 — PASS
- 代码：`DEFAULT_IN_FLIGHT_TTL = 120`；StateStore 启动时执行 `recover_stale_in_flight()` 把超 120s 的 in_flight 标为 failed；IdempotencyGuard.acquire 对 done/in_flight/failed 分别做 replay/block/retry。
- 实机：7 条 idempotency keys 全部 status=done，0 条 in_flight/failed，无卡死；两次 kickstart 后无残留。

### 5. production API key 权限 — PASS
- `PASSAY_API_KEY` sha256 == users 表 id=14 `pasay_bot_manager`（manager）的 hash；`PASSAY_ADMIN_API_KEY` sha256 == id=1 `admin` 的 hash；两 key 值不同；admin key 未被当作 PASSAY_API_KEY。
- `/opt/pasay-pm/pasay-telegram-bot/.env` 与 `/opt/pasay-pm/.env` 均 `-rw-------`（600, jhackuy）。
- 角色落地证据：audit 145 confirm actor=14（manager）、146 reverse actor=1（admin）；manager 访问 audit-logs 得 403，admin 得 200。

### 6. test income 完整 reverse — PASS
- #20（₱1.00）：status=reversed（confirmed_by=14 20:17:32，reverse 20:20:49 updated_by=1）。
- #21（₱2.00，之前 F11 验证）：reversed。
- #22（₱3.33，本次 F11 验证）：reverse 200 → status=reversed，audit 146 actor=1。
- 全库无 pending/confirmed 的测试收入；非 reversed 的 14 条均为真实租金或既有 DEV seed。

### 7. 无重复收入 — PASS
- amount=1.00 仅 #20 一条（已 reversed）；amount=3.33 仅 #22 一条（已 reversed）。
- confirm 仅生效一次：audit 对 #20 只有一条 confirm（#139）+ 一条 reverse（#140）；API 日志为 1×200 + 3×409（重复点击被 "Only pending income can be confirmed" 拒绝，不再写 audit）。本次 #22 同样 200/409/200 模式。
- audit 138/139/140 完整（#21: 141/142/143；#22: 144/145/146）。

### 8. 日志泄露 token/API key — PASS
- 对 pasay-bot.out/err.log、pasay-api.out/err.log、property-gateway 日志、start 脚本、两个 launchd plist 逐一 grep 三个 secret 的完整值：0 命中。
- 全 AI-Agent 日志正则扫描 `\d{8,10}:[A-Za-z0-9_-]{35}`：无 token 形态字符串。TimedOut 栈里无 URL/token。

## 附加实机发现

- **TimedOut 瞬态**：err.log 中唯一一次 `telegram.error.TimedOut`（httpcore.ConnectTimeout）发生在旧进程优雅停机时的 `_get_updates_cleanup`（PTB 关闭前最后一次 getUpdates），非稳态 polling；新进程 20:32 起轮询正常、无后续报错。属瞬态网络抖动，已自愈。PASS。
- **F12（Message is not modified）**：部署代码含 `handlers/edit_utils.py` `edit_message_text_idempotent`（仅吞 "Message is not modified"），已接入 callback.py `_edit()` 与 commands.py 3 处调用；bot 日志无 BadRequest/Conflict。PASS。
- **F11（audit old_value=old）**：重启后的 API 生效。证据：重启前 #20 audit #139 old_value.status=confirmed（旧 bug）；重启后 #21 audit #142 与本次 #22 audit #145 的 confirm old_value.status=pending（变更前），reverse #146 old_value.status=confirmed。PASS。
- **git 漂移（非清单项，需处理）**：部署在 /opt 的 `pasay_bot/config.py`、`main.py`（dotenv 显式装载规避 pydantic-settings pasay_ 前缀绑定问题 + 独立 getMe self-check 重构）**只存在于 /opt**（mtime 19:56–19:58），git 仓库工作区（18:42–19:07）没有这两处修改。若从 git 重新部署会回退掉该修复。

## 结论

**基本达到「可放心从 Telegram 完成真实收租」标准：8 项清单 7 项 PASS、1 项（token ownership）当前状态 PASS 但存在重启后冲突风险；F11/F12 均已实机验证生效，测试收入 #20/#21/#22 全部 reversed，无残留。**

**清除阻塞（1 个，必须处理）**：
1. `ai.hermes.property-gateway`：删除 `/Library/LaunchDaemons/ai.hermes.property-gateway.plist`（或 `launchctl disable system/ai.hermes.property-gateway`，并删除/轮换 `~/.hermes/profiles/pasay-property/.env` 中与 native bot 相同的 token），否则下次重启会出现双 consumer 抢 getUpdates → native bot 收租不可用。

**建议（非阻塞）**：
2. 将 /opt 上 `pasay_bot/config.py` + `main.py` 的改动提交回 feature/telegram-ui-v2，消除部署与 git 漂移。
3. 本次 F11 验证收入 #22 在 DB 保留为 reversed 审计痕迹（与 #20/#21 一致），idempotency keys 均 done；如需彻底删除可另行处理。
