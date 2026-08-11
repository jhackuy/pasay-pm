# V1.2.2 Phase C2 — Confirmed-Action Copilot Telegram UX Design (Hermes)

> Author: Hermes (orchestrator/UX) · Date: 2026-08-11
> Applies the task brief §3 (UX), §4 (low-input defaults), §5 (multilingual role UX), §13 (NL
> intent), §18 (failure UX). Backend contracts in `V122_C2_BRIEF.md`; the bot reads the canonical
> proposal via `POST /copilot/recommend` and executes via `POST /copilot/proposals/{id}/execute`.

## 1. UX Principles (from the brief, verbatim intent)

- Click, don't type. Auto-resolve everything. Default over config.
- **LLM proposes → user taps → system executes → role-aware feedback.** No raw proposal id / entity
  ref / enums / JSON / DB status shown to the user.
- Owner (zh): conclusion / risk / decision. Secretary (en): Action / Property / Tenant / Deadline /
  what counts as done. **Reorganize per receiver role — never translate the owner message.**
- Failure is human language, never `409 STALE_TARGET` / `error_code`.

## 2. User flow (Owner, zh)

```
🤖 TODAY card (existing) ──[为什么?]──▶ WHY card (existing) + [安排跟进]
                                    (suggestion row on each actionable item)

Suggestion tap:
🤖 建议
Unit 1608 租金严重逾期。
建议让秘书今天联系租客，确认付款日期。
[安排秘书跟进] [明天再提醒] [暂不处理]

[安排秘书跟进] tap → NOT yet executed. Call POST /copilot/recommend → confirmation card:
📋 准备安排跟进
房产：Sunset Tower · Unit 1608
事项：跟进逾期租金
负责人：Maria
截止：今天 17:00
秘书将收到英文任务通知。
[✅ 确认安排] [✏️ 修改] [取消]

[✅ 确认安排] tap → POST /copilot/proposals/{id}/execute →
✅ 已安排给 Maria（今天 17:00 截止）
她将在 Telegram 收到任务通知。我会继续跟踪这个事项。
[查看任务] [返回今日重点]
```

Failure variants (§18) replace the success card:
- target changed → `⚠️ 这个事项刚刚已经发生变化，我没有执行旧操作。已为你刷新最新状态。` + refresh button.
- telegram send retry → `✅ 任务已建立。通知秘书暂时失败，系统会自动重试。`
- duplicate callback → `✅ 这个操作已经执行过了。` (no second mutation)

## 3. Role-aware rendering (secretary receives its OWN card, not a translation)

Secretary English card (delivered to the secretary chat via the existing outbox/notifier):
```
📋 Follow-up Required
Unit: 1608
Issue: Rent overdue (PHP 85,000)
Action: Contact the tenant and confirm the expected payment date.
Due: Today, 5:00 PM
Owner: Ana P.
[✅ Done] [⏰ Remind me later] [📎 Add update]
```

The backend result block for an execution must include the receiver-resolved fields the two renderers
need. The bot builds the English secretary card from `display_context`/render block, never translating
the owner's Chinese text.

## 4. Low-input defaults (§4)

- Follow-up: default due = end of business day (today 17:00) unless item suggests otherwise; default
  assignee = property's default secretary (patron of the lease's property) else unique reasonable
  candidate; if >1 candidate or none → [秘书] / [Anna] / [我自己] inline pick, never a free-text field.
- Snooze: presets (reuse existing `_resolve_snooze_until`): 今天 / 明天(09:00) / 3天后 / 自定义 — the
  confirmation card ALWAYS shows the exact resolved time (§9).

## 5. Callbacks (add to `keyboards.py`, wire in `handlers/callback.py`)

Follow the existing `v1:action:entity:ref:nonce:ts` encoding. New actions:

| action | entity | ref | purpose |
|--------|--------|-----|---------|
| `cp_suggest` | `follow` / `assign` / `snooze` | proposal_id | tap suggestion → (recommend) → confirm card |
| `cp_confirm` | proposal_id | 0 | ✅ 确认安排 → execute |
| `cp_edit` | `due` / `who` | proposal_id | ✏️ show inline pick |
| `cp_decline` | proposal_id | 0 | 暂不处理 → cancel proposal |
| `cp_snooze_pick` | preset | proposal_id | choose snooze preset |
| `cp_assignee_pick` | assignee_idx | proposal_id | choose who |

Nonce+ts guard replay as elsewhere. Callback data stays ≤64 bytes ASCII.

## 6. i18n keys to add (both `zh` and `en` in `render/i18n.py`)

zh block:
```
'copilot.suggest_title': '🤖 建议',
'copilot.suggest_follow': '📞 安排秘书跟进',
'copilot.suggest_snooze': '⏰ 明天再提醒',
'copilot.suggest_dismiss': '暂不处理',
'copilot.confirm_title': '📋 准备安排跟进',
'copilot.confirm_yes': '✅ 确认安排',
'copilot.confirm_edit': '✏️ 修改',
'copilot.confirm_cancel': '取消',
'copilot.success_follow': '✅ 已安排给 {assignee}（{due}）\n她将在 Telegram 收到任务通知。我会继续跟踪这个事项。',
'copilot.role_property': '房产', 'copilot.role_topic': '事项', 'copilot.role_owner': '负责人',
'copilot.role_due': '截止', 'copilot.hint_secretary_note': '秘书将收到英文任务通知。',
'copilot.stale': '⚠️ 这个事项刚刚已经发生变化，我没有执行旧操作。',
'copilot.executed_already': '✅ 这个操作已经执行过了。',
'copilot.notify_retry': '✅ 任务已建立。通知暂时失败，系统会自动重试。',
'copilot.ask_who': '负责人：', 'copilot.ask_due': '截止时间：',
'copilot.who_me': '我自己', 
```
en block (secretary-facing + owner-en if any):
```
'copilot.followup_title': '📋 Follow-up Required',
'copilot.followup_action': 'Action:',
'copilot.followup_done': '✅ Done', 'copilot.followup_later': '⏰ Remind me later',
'copilot.followup_update': '📎 Add update',
```

## 7. Success/failure mapping (backend → human)

`/execute` response `detail`/`status` maps: `replay:true` → `copilot.executed_already`;
`notification PENDING/retry` → `copilot.notify_retry`; HTTP 409 with a resolvable error_code →
stale/target-changed human strings; confirm-time 409 → refresh card.

## 8. Verification target (Hermes runs after backend is green)

A real golden E2E on Telegram (§17): real overdue → TODAY → [安排秘书跟进] → confirm → DB task created
once → outbox → secretary English card received → owner success → ops center shows task → cleanup
(no real financial data touched). Snooze golden path with a short test window. Hermes owns this step
and reports transcripts in the closing report.
