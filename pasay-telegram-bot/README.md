# pasay-telegram-bot

Native Telegram bot layer for PASay-PM. Deterministic HTML-card UI +
InlineKeyboard for **今日管理首页 / 待处理 / 房源 / 财务 / 收租**; all financial
writes go through the Pasay PM API (`127.0.0.1:8000`) — this service never
writes to PostgreSQL directly.

## UX model (V1.1)

- `/start` = **今日管理中心** live dashboard: 本月租金(应收/已收/未收) +
  今日待处理(逾期·即将到期租约·待办任务) + 空置数, all from real API data
  (fetched with `asyncio.gather`). Empty/zero values are hidden; a clear day
  shows 「✅ 今天没有紧急事项」.
- Buttons over commands: `[💵 收租] [⚠️ 待处理] [🏘 房源] [📊 财务]`.
- **收租 3 击**: 首页 → 选未付款 Unit（逾期最前，已付/空置不显示）→ 确认。
  Smart defaults: 账期=当月, 日期=今天, 金额=当前应收, 方式=最近一次使用
  (user-level default stored in SQLite). 最终确认不可跳过: `[✅确认][✏️修改][❌取消]`.
- **状态驱动按钮**: 未付→`[✅登记收租]`; 已付→`[💰查看付款]`; 已撤销→`[🔄重新登记]`;
  空置/无活跃租约→不显示收租按钮; 过期/已收的旧卡片不再写入。
- **edit-first**: 导航回调 edit 原消息（`edit_message_text_idempotent`），不刷屏;
  命令发起或财务写成功才 send 新消息。每次导航先 `answerCallbackQuery`。
- **每页有返回/取消**: 二级页 `[🏠首页]`, 写操作中途 `[❌取消]`;
  过期操作显示「⚠️ 这个操作已经过期」+ `[🏠返回首页]`。
- **错误可恢复**: 加载失败 → `[🔄重试][🏠首页]`; 财务写失败 → 同 nonce 重试
  （idempotency 去重）; 超时写 → 自动 reconcile 最终状态, 绝不重复创建。
- **空状态**: 「🎉 暂无逾期租金」「🏘 还没有房源数据」「✅ 今天没有紧急事项」,
  一律带首页按钮, 无 "No data"/[]/0 records。
- 金额一律 PHP 原币 `₱` + 千分位, 无 0 尾巴, 负数/0.01/大额均正确。

## Hard requirements implemented & tested

- per-card nonce + SQLite idempotency (`in_flight`/`done`/`failed`),
- double-click confirm writes exactly once,
- backend timeout reconciliation via `GET /incomes/{id}` + `find_income`
  (never claims "nothing changed", never duplicates),
- 15-minute card expiry + backend-state arbitration for stale buttons,
- RBAC (OWNER `5177241442` full; SECRETARY `1083657401` records income but
  cannot confirm/finalize) with UI hiding + backend enforcement,
- callback_data `<=64B` (`v1:<action>:<entity>:<ref>:<nonce>:<ts>`),
- `html.escape()` everywhere; `<=4096` UTF-16 message truncation; Decimal
  money formatting (no floats).

Hermes NLU adapter is intentionally NOT wired up; free text is routed by
deterministic keyword matching (`nl_bridge.py`), otherwise the bot replies
"use the buttons / /help".

## Run

```bash
cd pasay-telegram-bot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # fill PASSAY_TG_BOT_TOKEN / PASSAY_API_KEY
.venv/bin/python -m pasay_bot.main --dry-run   # getMe self-check, no polling
.venv/bin/python -m pasay_bot.main             # start polling
```

launchd deployment / cutover is handled by the orchestrator;
`bin/start-native-bot.sh` is the fail-closed wrapper.

## Tests

```bash
cd pasay-telegram-bot
env -u PYTHONPATH .venv/bin/python -m pytest tests -q   # bot suite (>=101)
cd .. && env -u PYTHONPATH .venv/bin/python -m pytest tests -q  # backend (102)
```

Note: run bot commands with `PYTHONPATH` unset if the environment sets one
(this machine's shell exports the Hermes venv path, which would shadow the bot
venv's own deps).

## Layout

- `pasay_bot/keyboards.py` — callback_data encode/decode single source of truth.
- `pasay_bot/api_client.py` — typed httpx client (only writer to the API).
- `pasay_bot/render/` — `html.py` (escape/money/truncate/pagination),
  `cards.py` (all message cards incl. dashboard/pending/collect),
  `i18n.py` (zh + en full copy sets).
- `pasay_bot/handlers/` — `commands.py`, `callback.py`, `conversation.py`,
  `nl_bridge.py`.
- `pasay_bot/state/` — SQLite conversations + idempotency keys + user defaults.
- `tests/` — bot suite incl. ★ defense cases and the V1.1 UX scenarios
  (`test_ux.py`).
- `ux/` — `UX_AUDIT.md` (Phase A), `UX_REVIEW2.md` (Phase D second review).
