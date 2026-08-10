# Phase A — 生产切流基线记录（rollback 用）

日期:2026-08-10。执行:生产前 cutover (feature/telegram-ui-v2 @ 9d70739, bot 99 / backend 102 全绿)。

## 生产 token 与 consumer
- **@zhushoumacbot** (882050..., ~/.hermes/.env, 主 DM + 群 -5417146216)
  - consumer = 主 Hermes gateway PID 50213, launchd `ai.hermes.gateway`, ProgramArguments=`... -m hermes_cli.main gateway run`
  - **保持现状，绝不动。**
- **@pasayhousebot** (877703..., ~/.hermes/profiles/pasay-property/.env, 受限群 -1004433994558 "PASay-PM")
  - consumer = pasay-profile gateway **PID 70760**, launchd `ai.hermes.property-gateway`
  - ProgramArguments = `/Users/jhackuy/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile=pasay-property gateway run -v`
  - EnvironmentVariables = HOME=/Users/jhackuy, HERMES_HOME=~/.hermes/profiles/pasay-property
  - **本 cutover 目标：由新的 pasay-telegram-bot 接管该 token。**

## 权限 (pasay-property 保持)
- allow_from(群+DM): 5177241442 (OWNER), 1083657401 (SECRETARY)
- 群: -1004433994558

## ai-controller
- `com.ai-controller.bot.plist` 存在但**未加载/未运行**（launchctl print 无此 service；bot.py 无进程）。不构成 409。

## 原生 Bot 将接管 @pasayhousebot
- 部署: /opt/pasay-pm/pasay-telegram-bot（同 /opt/pasay-pm 部署风格）
- config: PASSAY_TG_BOT_TOKEN=<@pasayhousebot token>, PASSAY_API_BASE=http://127.0.0.1:8000/api/v1,
  PASSAY_API_KEY(manager), PASSAY_ADMIN_API_KEY(admin), STATE_DB=/opt/pasay-pm/pasay-telegram-bot/state/bot_state.db
- launchd: `ai.payay.telegram-bot` (RunAtLoad + KeepAlive + logging)  — 待 Phase E 建，先临时进程验证
- 角色: OWNER 5177241442 / SECRETARY 1083657401（= 原网关允许的用户，保持一致）

---

## ROLLBACK（切回原 pasay-profile gateway）
```bash
# 1) 停止原生 bot（临时进程或 launchd）
sudo launchctl bootout system/ai.payay.telegram-bot   # 若已注册 launchd
pkill -f "pasay_bot.main"                             # 若临时进程
# 2) 恢复 pasay-profile gateway polling @pasayhousebot
sudo launchctl bootstrap system /Library/LaunchDaemons/ai.hermes.property-gateway.plist
#   或 bootout + bootstrap 重启
# 3) 验证 @pasayhousebot 恢复由 Hermes 应答
curl -s "https://api.telegram.org/bot$PASSAYHOUSE_TOKEN/getMe"   # ok
# 4) 检查无 409（Hermes 成为唯一 consumer）
```
> 原生 bot 未做 DB migration / schema 变更；SQLite state 在 /opt/pasay-pm/...，删除即回到干净态。生产 API/PG 未动。
