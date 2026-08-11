# V1.2.2 C1.1 — Telegram Fast-First UX Design (Hermes-owned)

> Maps the C1.1 backend (deterministic-first TODAY + WHY/ASK enrichment) onto the
> native `pasay-telegram-bot` card. Hermes owns this. Requirement 3.

## 1. Fast-first TODAY card
User taps 🤖运营助手 (or `/copilot`) → bot calls the **deterministic `/today`**
(no LLM) → renders immediately:

```
🤖 <b>今日重点</b>
📅 2026-08-11

🔴 严重逾期租金 (lease…)
   Unit 1203 · 已逾期 7 期 · ₱455,000
🟠 租约 7 天内到期
   Unit 405 … 8/18 到期
🛠 已逾期维护
   AC 保养 …

[1 为什么?] [2 为什么?] [3 为什么?]
[问运营助手]
```
- Each row = deterministic `label`/`reason` + amounts via H.money, NO internal refs.
- Emoji by kind (reuse `_copilot_emoji`).
- Primary items ≤3 (deterministic top-K), summary line from deterministic renderer.
- Secondary items (4..N) behind a `查看全部` (progressive disclosure) — optional.

## 2. Inline buttons → on-demand LLM (requirement 3)
- `[N 为什么?]` per item → `POST /operations/copilot/why {item_ref}` → edit the same
  message (or reply) with the grounded explanation + recommendation under a
  `[为什么?]` button that collapses back to TODAY.
- `[问运营助手]` → reply with `问运营助手：<你的问题>` prompt; user types a question →
  `POST /operations/copilot/ask`. Reuse a `ConversationHandler`-style flow (ASK mode)
  or a simple "waiting for question" state.
- While `why`/`ask` LLM runs (15–45s), show a Telegram-friendly processing note:
  `⏳ 分析中…` — do NOT block the TODAY card (the card is already shown).
- BUTTON `callback_data`: reuse `encode()` with new actions `ACTION_COPILOT_WHY=cpw`
  (payload `cpw:<item_ref>`) and, for ask, an inline `ACTION_COPILOT_ASK=cpa`.

## 3. Failure fallback (requirement 8)
- If `/today` backend errors (should be near-impossible since deterministic): show the
  existing `⚠️ load_error` + retry + home; never fabricated.
- If `why`/`ask` LLM provider down: backend returns the deterministic fallback
  (HTTP 200, `fallback:true`) — bot just renders it normally (still grounded).
- If `why`/`ask` genuinely errors (network): bot shows friendly
  `⚠️ 运营助手分析暂不可用，请稍后再试` + back to TODAY. Operations center (待办中心)
  unaffected.

## 4. Latency (requirement 4)
- E2E measure:
  A. TODAY warm (fast path) — target < 1s backend, < 3s to user.
  B. TODAY cold (fresh context build) — still < 2s.
  C. WHY (LLM explain) — record p50/p95 over repeats.
  D. Q&A — record p50/p95.
  E. provider-timeout fallback — force provider down, TODAY must stay fast + correct;
     WHY returns deterministic fallback.
- Backend returns `context_build_ms/priority_ms/llm_ms/total_ms`; bot can log/record them.

## 5. Files to touch (after backend lands)
- `api_client.py`: `copilot_today_fast()`, `copilot_why(item_ref)`, `copilot_ask(question)`.
- `render/cards.py`: `copilot_today_card(today_fast, has_input=False, ...)` + `copilot_why_card(...)`.
- `handlers/commands.py`: rework `show_copilot` to call fast path; add why/ask handlers.
- `handlers/callback.py`: handle `ACTION_COPILOT_WHY` / `ACTION_COPILOT_ASK` / collapse.
- `keyboards.py`: TODAY buttons + why/ask buttons.
- `render/i18n.py`: zh/en keys (why_button, ask_button, analyzing…, ask_prompt).

## 6. Acceptance
- Tap 运营助手 → no text input → TODAY appears fast (measured).
- `[为什么?]` → LLM explanation, grounded, no refs/model leak, returns to TODAY.
- Break provider → TODAY still fast+correct; WHY friendly fallback.
