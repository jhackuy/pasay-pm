# PRODUCT_RULES.md

Verified business truths the Pasay system enforces. Authoritative and concise.

---

## 1. Truth hierarchy

- **Operation is the business record (Truth).**
- **Task is at most one current human-action projection** of an Operation.
- **Reminder / Reply / Notification NEVER mark completion.** They are signals, not closures.
- **Task status MUST NOT reverse business truth.** An Operation's state is changed only by verified real-world events; a Task cannot flip a CLOSED Operation back to OPEN, nor push an OPEN Operation to CLOSED on its own.

---

## 2. Money ≠ status equivalences (forbidden inferences)

The system MUST treat the following as **distinct** states and must never collapse one into another:

| False equivalence | Reason it is forbidden |
|---|---|
| Quote ≠ Expense | A quote is an offer; no money has moved and no purchase has occurred. |
| Approval ≠ Payment | An approved expense has not had funds leave the account. |
| Payment Claim ≠ Verified Payment | A tenant or payer's self-report is not money received; verification requires reconciled evidence. |
| Partial Rent ≠ Paid | A partial payment does not satisfy the rent obligation. |
| Reject Quote ≠ Repair Closed | A rejected quote leaves the repair OPEN awaiting another quote or cancellation. |

**Repair closes only after completion is verified.** Money in flight (paid quotes, paid expenses) does not close the Repair itself.

---

## 3. Money precision

- **DB column type**: `Numeric(14, 2)`.
- **Python type**: `Decimal`. **NEVER `float`.**
- **JSON serialization**: string, never number — to preserve precision across every boundary.
- **Parsing**: parse user input via `Decimal(str(value).replace(',', ''))`, then `quantize(Decimal('0.01'))`.
- **Negative amounts** are forbidden unless the column is explicitly `deduction_amount` or `refund_amount`.

---

## 4. Time

- **DB column type**: `timestamptz`.
- **Python type**: `datetime` with `timezone.utc`.
- All `*_at` columns are server-default `now()`.
- All cron / scheduler math is UTC. No local-time arithmetic in business logic.

---

## 5. Permission boundary

- Authorization is enforced on **Organization + Membership only (fail-closed)**. Missing membership ⇒ no access.
- **Roles**: `OWNER`, `SECRETARY`.
- **Principal types**: `HUMAN`, `SERVICE`, `AI_AGENT`, `SYSTEM`.
- **API credential lookup**: HMAC-SHA256 hash. Raw keys are never stored, logged, or returned.
- **Cross-org read or write MUST 403** even with a valid credential. Org boundary is absolute.
- **Last-Owner protection**: removing the last `ACTIVE` `OWNER` of an Organization is forbidden.

---

## 6. Idempotency

- Every write endpoint accepts an `Idempotency-Key` header.
- Every `claim`, `payment`, `expense`, and `income` table carries a partial-unique index on `(org_id, idempotency_key)`.
- Replays within the key TTL return the **stored response** without re-running the mutation. Side effects are guaranteed at most once per key.

---

## 7. State machine invariants

Each entity has exactly one row in its lifecycle. Forbidden transitions are rejected.

- **Repair** (9 states):
  `OPEN → ASSIGNED → QUOTED → APPROVED → IN_PROGRESS → COMPLETION_CLAIMED → VERIFIED → CLOSED`, plus terminal `CANCELLED`.
  Repairs in `QUOTED | APPROVED | IN_PROGRESS | COMPLETION_CLAIMED` are **not CLOSED** even if an associated expense is paid.
- **Lease**: `active | expired | terminated`. `terminated` requires the move-out settlement to be RECONCILED.
- **MoveOutInspection**: `SCHEDULED | INSPECTED | CONFIRMED | CANCELLED`. `CONFIRMED` triggers DepositSettlement.
- **DepositSettlement**: `DRAFT | CONFIRMED | RECONCILED`. `RECONCILED` is terminal.
- **RentPaymentClaim**: `PENDING | VERIFIED | FAILED | REVERSED`. `FAILED` / `REVERSED` may re-create a new claim.
- **ExpensePaymentClaim**: same lifecycle as RentPaymentClaim.

---

## 8. Telegram UX contract

- **Reply keyboard** is exactly 3×2:
  - Row 1: `Home`, `Properties`
  - Row 2: `Tasks`, `Rent`
  - Row 3: `Expense`, `Archive`
- **Default language**:
  - OWNER: `zh-CN`.
  - SECRETARY: `en-US`.
- **Callback data cap**: 64 bytes. **Versioned prefix** `v1:`. Unknown versions → ignored with a `已过期` answer callback.
- **Deterministic fast paths**: callback → handler lookup, no LLM on the happy path.
- **LLM fallback**: at most **one** per unclear business intent. Provider: MiniMax.
- **Group chat silence**: the bot MUST NOT reply in groups unless explicitly invoked or a real business signal exists.
- **Every click / callback / permission / expired / error path MUST return explicit feedback.** No silent failures.

---

## 9. Regression invariants (must-have tests)

These behaviors are locked by tests. Any regression is a release blocker.

- Unit `7777` + tenant name + Philippine phone in a Telegram natural-language update → the tenant is updated; **no expense is created**.
- Telegram ID `5177241442` → `OWNER` (default `zh-CN`). Telegram ID `1083657401` → `SECRETARY` (default `en-US`). Unknown IDs → sensitive actions denied.
- Re-confirming a `VERIFIED` income (idempotent replay or duplicate submit) → API returns **409**; the bot renders the prior `VERIFIED` state.
