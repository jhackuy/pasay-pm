# Identity Audit — C2 Secretary / Owner / Operator / AI-Agent Model

> Date: 2026-08-11 · Author: Hermes (Lily), read-only audit · No mutations performed.
> Trigger: user correction — "there is NO real secretary named Maria; Maria is the old name abandoned
> for Lily; do NOT bind chat 1083657401 to maria(7); audit before changing anything."

## TL;DR verdict

- `maria`(user 7) is **NOT** a real current human secretary. It is a **legacy commission-referral
  identity** from Phase 2 (baseline "Maria Referral 5%", one confirmed settlement ₱3,250 on lease 3).
  It is **not wired into any production code path, no API key, no telegram chat id, no operational
  tasks, never an audit actor.** Its name is a leftover from the era when the AI agent was called
  "Maria"; that name is now "**Lily**".
- `pasay_bot_manager`(user 14) is the **active secretary/operator SERVICE ACCOUNT** bound to the
  secretary Telegram chat `1083657401`. It is a **manager-role backend identity used by the native
  bot** (`@pasayhousebot`) for all non-admin operations, and it is the **outbound delivery target**
  for secretary-role notifications. It is a service/operator account, not a named human "Maria".
- `admin`(user 1) is the **OWNER's** backend identity (telegram `5177241442`, admin role, admin API
  key). Proactive business-task notifications currently fall back to it
  (`OPERATIONS_DEFAULT_ASSIGNEE` unset → defaults to 1).
- No change was made. Anything that touches identity must be gated on this corrected understanding.

---

## 1. Current backend `users` (raw, from production DB `pasay_pm`)

| id | username        | role    | active | telegram_chat_id | created_at      | notes |
|----|-----------------|---------|--------|------------------|-----------------|-------|
| 1  | `admin`         | admin   | t      | `5177241442`     | 2026-08-09 23:51 | OWNER backend identity, admin key |
| 7  | `maria`         | agent   | t      | *(none)*         | 2026-08-10 07:51 | **legacy referral agent** (Phase 2) |
| 12 | `dev_agent_maria`| agent  | t      | *(none)*         | 2026-08-10 16:48 | DEV-marked test agent |
| 13 | `dev_agent_john`| agent   | t      | *(none)*         | 2026-08-10 16:48 | DEV-marked test agent |
| 14 | `pasay_bot_manager`| manager| t    | `1083657401`     | 2026-08-10 19:53 | **secretary-chat service/operator account** |

- `users_id_seq = 16`; ids 2–6, 8–11, 15, 16 were created over time and **hard-deleted** (no soft-delete
  column). Audit retains actors 15/16 (`copilot_context_built`, `copilot_proposal_created/confirmed`) —
  transient C1/C1.1 copilot test accounts now gone.

## 2. What each identity actually represents TODAY (evidence)

### `maria` (7) — LEGACY, NOT a current human secretary
- **Zero** operational_tasks, recurring_rules, V1.1 tasks, or audit rows as actor.
- **One** confirmed `commission_settlement`s (id 7, agent 7, lease 3, ₱3,250, confirmed 2026-08-10).
  This is the Phase-2 financial baseline "Referral" recipient (`PHASE2_CLOSING_REPORT.md`: *1 Agent(maria)
  / Commission Rule "Maria Referral 5%" / settlement #7 confirmed ₱3,250*).
- **No API key** matches the bot's `PASSAY_API_KEY`/`PASSAY_ADMIN_API_KEY` (those resolve to 14 and 1).
- **Not referenced anywhere in production Python** (`grep maria app/ pasay-telegram-bot/pasay_bot/` → no hits).
- The name "Maria" in UX-design markdown (`V122_C1_UX_DESIGN.md:39`, `V122_C2_UX_DESIGN.md`) was
  **example renderer copy** I wrote, echoing the legacy DB name — it is NOT a hard binding and confirms
  the name is only a leftover label.

### `pasay_bot_manager` (14) — ACTIVE secretary/operator SERVICE ACCOUNT
- Native bot `@pasayhousebot` uses `PASSAY_API_KEY` → resolves to **user 14** for all normal ops.
- `copilot_runs`: **65** all by actor 14 (it is the operating account for the copilot/manager flow).
  Audit: actor 14 did `copilot_context_built` 65, `confirm` 5, `create` 3.
- `telegram_chat_id=1083657401` == the SECRETARY Telegram chat (per roles.json & bot roles.py).
  → This is why outbound secretary-role notifications are delivered: `resolve_recipient` maps
  manager user (14) → chat 1083657401.
- **It is a service/operator account, not a named human "Maria".** Whether it is manned by a human
  operator or the AI agent acting on behalf of the operator is a policy question (see §6).

### `admin` (1) — OWNER
- `PASSAY_ADMIN_API_KEY` → user 1. Owner telegram `5177241442`. Full admin privileges
  (confirm/reverse/approve). Proactive business-task notifications default to it
  (`OPERATIONS_DEFAULT_ASSIGNEE` unset → 1).

## 3. Inbound identity resolution (as deployed)

- **Backend auth** = API key only (`users.api_key_hash`); every API call is tied to exactly one backend
  `User` via its key. There is **no** Telegram-id→backend-user mapping in the API.
- **Bot layer** (`roles.py` / `roles.json`) maps Telegram ids → *role* for UI + fast-refusal:
  `5177241442→OWNER`, `1083657401→SECRETARY`. The bot then calls the backend using 14's key
  (normal) / 1's key (admin). So the *bot* decides who is Owner vs Secretary; the *backend* enforces
  role via the API key's user.

## 4. Outbound task delivery (as deployed)

- `resolve_recipient` → `assigned_user_id → telegram_chat_id`. Current PENDING tasks:
  - RENT/LEASE → assignee `admin`(1, chat 5177241442) → **owner gets proactive business reminders**
    (`OPERATIONS_DEFAULT_ASSIGNEE` unset).
  - 2 `SETTLEMENT_PENDING` → `dev_agent_john`(13, **no chat id**) → failed/dropped (outbox rows 17/18
    FAILED "no telegram chat id").
  - Secretary/follow-up tasks reach chat `1083657401` **only when explicitly assigned** to a
    secretary/mgr identity (e.g. user 14). There is **no configured "secretary" default** — the C2
    builder returned "multiple assignee candidates" and required an explicit pick.

## 5. What is stale / legacy (safe-cleanup candidates, NOT performed)

| Identity | Status | Live references | Safe-cleanup plan (future, not now) |
|---|---|---|---|
| `dev_agent_john` (13) | DEV test data | 2 pending tasks, 3 settlements, 2 failed outbox | Retire via `scripts/dev_cleanup.py` (DEV-marked). Suppresses 2 failed outbox rows. |
| `dev_agent_maria` (12) | DEV test data | 2 settlements | Same cleanup script. |
| `maria` (7) | **legacy referral agent**, superseded name | 1 confirmed commission settlement (financial, must NOT be deleted) | **Do NOT delete** — it is a financial commission recipient. Set `is_active=false`(if ever needed) + keep settlement intact; or re-home the settlement to a real agent if the owner decides `maria` should never appear. Requires Owner decision. |
| users 15,16 (absent) | already removed | only audit remnants | None. |

**Key rule:** `maria`(7) holds a **confirmed financial commission settlement** — it is **not** free
deletable junk. Any retirement must keep financial integrity (never DELETE financial rows; 财务禁删).

## 6. Proposed canonical identity model (recommendation; no change applied)

| Logical role | Canonical backend identity | Telegram | Notes |
|---|---|---|---|
| **Owner** | `admin` (user 1, role admin) | `5177241442` | Admin API key; confirm/reverse/approve; proactive business reminders default to this. |
| **Real human secretary / operator** | `pasay_bot_manager` (user 14, role manager) | `1083657401` | Service/operator account the native bot acts through; outbound secretary-role deliveries target its chat. Keep `OPERATIONS_DEFAULT_ASSIGNEE=14` if secretary should own business reminders (currently owner). |
| **Hermes / Lily AI agent** | **NO separate DB `users` row** | — (acts via the Hermes gateway / owner or operator account) | AI agent must NOT be a `users` row / human secretary. Lily operates through the gateway bot; identity/authority = the API key it uses (owner/operator), never a `maria`-style named person row. |
| **Telegram bot/service identity** | `@zhushoumacbot` (Hermes gateway, full), `@pasayhousebot` (native pasay bot) | — | Bots are delivery endpoints, not human users. |
| **Legacy referral agent** | `maria` (user 7) | — | Retain as a financial-commission identity only; rename/supersede requires Owner decision; never bind to a telegram chat as a "human secretary." |

### Concrete recommendations (not executed)
1. Do **not** bind `1083657401` to `maria`(7). Correctly, it belongs to the **secretary/operator
   service account** `pasay_bot_manager`(14) — which already holds it.
2. If `maria`'s commissioned ₱3,250 should reflect a current recipient, **Owner decides** whether to
   (a) keep `maria` as a dormant referral identity (recommended default — financial integrity), or
   (b) re-home that commission to a real agent. Either way **no financial DELETE**.
3. Consider setting `OPERATIONS_DEFAULT_ASSIGNEE=14` so proactive secretary-role reminders target the
   secretary chat (currently they default to the owner). Confirm intent with Owner.
4. Retire `dev_agent_john`/`dev_agent_maria` via the DEV cleanup script (clears 2 failed outbox rows).
5. Fix the AI-agent naming everywhere (docs/UX renderers) to **Lily**; remove "Maria" as secretary-
   example copy so it can't be mistaken for a real identity.

## 7. Constraints honored
- Read-only audit — **no rename, no delete, no mutation**.
- **No financial mutation.**
- **No C3** (no autonomous behavior).

---

*Prepared by Hermes (Lily). Awaiting Owner confirmation before any identity change.*
