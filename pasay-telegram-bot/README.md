# pasay-telegram-bot

Native Telegram bot layer for PASay-PM (Phase 2 implementation of
`NATIVE_BOT_DESIGN.md`). Deterministic HTML-card UI + InlineKeyboard for
**房源 / 财务 / 逾期 / 收租**; all financial writes go through the Pasay PM API
(`127.0.0.1:8000`) — this service never writes to PostgreSQL directly.

## Scope (this phase)

- Deterministic pages: `/properties` `/finance` `/overdue` `/rent` + main menu.
- Full rent-entry flow: property → unit → amount → date → method → **二次确认** →
  `POST /incomes {status:pending}` → `POST /incomes/{id}/confirm`.
- Hard requirements implemented & tested:
  - per-card nonce + SQLite idempotency (`in_flight`/`done`/`failed`),
  - double-click confirm writes exactly once,
  - backend timeout reconciliation via `GET /incomes/{id}` (never claims
    "nothing changed"),
  - 15-minute card expiry + backend-state arbitration for stale buttons,
  - RBAC (OWNER `5177241442` full; SECRETARY `1083657401` records income but
    cannot confirm/finalize) with UI hiding + backend enforcement,
  - callback_data `<=64B` (`v1:<action>:<entity>:<ref>:<nonce>:<ts>`),
  - `html.escape()` everywhere; `<=4096` UTF-16 message truncation; Decimal
    money formatting (no floats).

Hermes NLU adapter is intentionally NOT wired up this phase; free text is
routed by deterministic keyword matching, otherwise the bot replies "use the
buttons / /help".

## Run

```bash
cd pasay-telegram-bot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # fill PASSAY_TG_BOT_TOKEN / PASSAY_API_KEY
.venv/bin/python -m pasay_bot.main --dry-run   # getMe self-check, no polling
.venv/bin/python -m pasay_bot.main             # start polling
```

launchd deployment / cutover is handled by the orchestrator (Phase 4+);
`bin/start-native-bot.sh` is the fail-closed wrapper (env load → state DB →
getMe self-check → `exec`).

## Tests

```bash
cd pasay-telegram-bot
.venv/bin/python -m pytest tests          # bot suite
cd .. && .venv/bin/python -m pytest tests # backend suite (102) unchanged
```

Note: run bot commands with `PYTHONPATH` unset if the environment sets one
(this machine's shell exports the Hermes venv path, which would shadow the bot
venv's own deps).

## Layout

- `pasay_bot/keyboards.py` — callback_data encode/decode single source of truth.
- `pasay_bot/api_client.py` — typed httpx client (only writer to the API).
- `pasay_bot/render/` — `html.py` (escape/money/truncate/pagination),
  `cards.py` (all message cards), `i18n.py` (zh full, en menu+status).
- `pasay_bot/handlers/` — `commands.py`, `callback.py`, `conversation.py`,
  `nl_bridge.py`.
- `pasay_bot/state/` — SQLite conversations + idempotency keys (std-lib).
- `tests/` — 80 tests incl. the ★ defense cases (double confirm, expired
  callback, permission bypass, timeout before/after write, idempotency).
