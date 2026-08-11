# V1.2.2 Phase C1 — 运营助手 Telegram Card UX Design (Hermes-owned)

> This is Hermes's UX blueprint (requirement 3: Hermes owns product UX). It maps the
> backend C1 TODAY response to the native `pasay-telegram-bot` card, satisfying the UX
> hard metrics (requirement 5). Codex Max's backend is at
> `POST /api/v1/operations/copilot/today`.

## 1. Hard-metric mapping (req 5)

| UX hard metric | How the card satisfies it |
|---|---|
| Opens, no typed input → results | A button/command `/copilot` (and dashboard →「🤖 运营助手」button) triggers `POST /copilot/today` with empty body. Zero free-text needed. |
| First screen shows ≤ 3 most important items | Card renders exactly `top_items[:3]`, each one compact block. |
| Each item readable in seconds: 什么事/为什么重要/建议做什么 | Each block = one line `[emoji] 标题` + `为什么:` + `建议:`. Short, imperative, no AI prose. |
| Secondary items progressive disclosure | Items 4..N hidden behind a `「查看全部 N 项」` button → secondary message (reuse Pagination/Outbox pattern). |
| No backend fields / IDs / JSON / model terms leaked | Card only surfaces human text: title, reason, suggested action, amount (money-formatted), date. Never `task:{id}` refs visible — refs stay in the API/button callback only. |
| No long AI analysis normally | `summary` ≤ ~2 sentences shown once at top; never a wall of reasoning. Each item's text ≤ ~1 line reason + ~1 line action. |

## 2. Backend TODAY response (expected shape from C1 brief)

```
{
  "top_items": [
    {"item_ref": "lease:42", "title": "...", "reason_why_important": "...", "suggested_action": "...", "kind": "overdue|expiring|maintenance|task", "amount": "65000.00", "date": "..."},
    ... ≤3 ...
  ],
  "summary": "2 overdue rents, 1 lease expiring this week.",
  "context_schema_version": "1.0",
  "provider": "deepseek",
  "model": "deepseek-v4-pro",
  "latency_ms": 123
}
```
Hermes will VET the actual response when Max delivers and adapt.

## 3. Card block per item (zh, HTML, escaped)

```
🔴 <b>Unit 1203 · Maria</b>
为什么：租金逾期 2 期，欠 PHP 130,000
建议：今天跟进收租，避免继续拖欠

📋 <b>Unit 405 · Juan</b>
为什么：租约 8/18 到期（本周）
建议：联系续租或安排新房客

🛠 <b>空调保养 · Unit 1203</b>
为什么：例行维护
建议：安排上门保养
```
- Emoji by `kind`: 🔴 overdue · 📋 expiring · 🛠 maintenance · ✅ task/todo · ⚠️ other.
- Amount via `H.money()` (PHP thousands), date via `H.format_date()`.
- All dynamic text through `html.escape`.
- Header: `🤖 <b>运营助手</b>` + `📅 <今天>` + `provider: <model>` only in a *subtle* footer line or DEBUG flag — DO NOT expose model terms to the end user. (Decision: keep model line out of the user-visible card; log it instead.)

## 4. Empty state
- `No pending items today. ✅` — positive, short.

## 5. Error state
- Provider unavailable/503/timeout → `⚠️ 运营助手暂时不可用，请稍后再试` + no fabrication. Reuse `error_keyboard`.

## 6. Progressive disclosure
- Secondary items (if any) in a lighter follow-up message under a `「查看全部」` button. Items 4..N shown as single-line `[emoji] 标题 · 为什么` (no action line) to keep it scannable.

## 7. Entry points (no rework of existing dashboard)
- Add command `/copilot` + a dashboard home button `🤖 运营助手` that calls `POST /copilot/today` (empty body) and renders this card.
- Guarded by existing RBAC (operations permission), same as /ops.

## 8. Files to touch (after Max lands the endpoint)
- `pasay_bot/api_client.py`: `copilot_today() -> TodayResponse` typed client.
- `pasay_bot/render/cards.py`: `copilot_today_card(...)`.
- `pasay_bot/handlers/commands.py`: `cmd_copilot` + `show_copilot` page builder.
- `pasay_bot/keyboards.py`: optional「查看全部」keyboard + home button.
- `pasay_bot/render/i18n.py`: zh/en keys.

## 9. E2E scenario to prove deterministically (req 6)
Seed one unit with severe overdue (≥2 periods), one active lease expiring ≤7d, one ordinary maintenance task (long title/rich note), one low-amount todo (low value). Assert card shows the severe overdue FIRST and the low-amount/long-note item NOT above it. This is the anti-"long-text-beats-risk" proof.
