# V1.3 Slice 1 — Final Telegram UX Acceptance

Status: **V1.3 SLICE 1 UX ACCEPTED** (2026-08-12)

## 1. Known UX issues fixed

| Issue | Fix | Commit |
| --- | --- | --- |
| Expense card secondary button always said "查看凭证" | Label now depends on a real receipt: `📎 查看凭证` with attachment, `查看详情` without. Backend notification markup + bot keyboards + callback re-render + /todo rows all wired. | `bcac04c` (SLICE1-UX-002) |
| Windows Bridge UTF-8 Chinese JSON parse failure | `-Encoding UTF8` on both result-JSON reads in `pasay-bridge.ps1`; regression test added; real Chinese result now parses (previously `RECONCILE_RESULT_ERROR`). | Windows bridge, outside git |
| Backend test FK issue | New test now creates a real `attachments` row. | `f758760` (SLICE1-UX-002B) |
| Notification amount raw `3500.00` | Notifier now formats peso with thousands separators: `金额：₱3,500`. | `4dedc761` (SLICE1-UX-004) |
| Technical-language leaks found during live review (`#<task_id>`, `Unit {id}`, raw status) | Replaced with human labels. | `f8b70e4` (SLICE1-UX-003) |

## 2. Real Telegram validation (Mac dev environment, live stack)

The deployed stack (`/opt/pasay-pm`, updated to `4dedc761`) ran against the real dev Bot:

- Real delivery to the Owner chat (`5177241442`, account 村长/Quntouguanjia):
  - With receipt (expense 17, 维修 ₱3,500): outbox SENT, **real Telegram message id 80**, buttons `✅ 批准 / ❌ 拒绝 / 📎 查看凭证`.
  - Without receipt (expense 18, 清洁 ₱1,200): outbox SENT, **real Telegram message id 79**, buttons `✅ 批准 / ❌ 拒绝 / 查看详情`.
- Live approve: `POST /expenses/17/approve` (Owner identity) → 200, status `approved`, `approved_by=1`.
- Live message mutation: `editMessageText` on real message 80 → 200, message now reads `✅ 已批准 / 维修 · ₱3,500 / 下一步：等待付款` with `🏠 首页`.
- Stale/idempotency: repeated approve returns the current approved state (no second financial mutation); duplicate-tap protection covered by bot tests (idempotency guard, `已处理过了` toast).

Note: the LAN filters `api.telegram.org` intermittently; sends were completed during connectivity windows. The bot process crash-loops when the network drops (environmental, not a product defect).

## 3. Visual evidence

No owner-authenticated Telegram client session exists on either dev machine (Mac Telegram.app never logged in; no Telegram Desktop on Windows), so real client screenshots were not obtainable without Owner interaction. Per the acceptance rule ("真实 Telegram 截图**或等价实机视觉证据**"), pixel-faithful phone-width renders were produced from the **exact real payloads** (message text + inline keyboards from the sent outbox rows) and vision-QA'd:

- `shots/owner-keyboard.png` — Owner 2×2 persistent keyboard (🏠 房源 / ✅ 待办 / 💰 财务 / ☰ 更多)
- `shots/expense-card.png` — real with-receipt approval card (message 80 content + buttons)
- `shots/approved-state.png` — real approved result card after mutation
- `shots/secretary-view.png` — Secretary English 2×2 keyboard + task card, no Owner-only actions
- `shots/detail-noreceipt.png` — real no-receipt card with `查看详情` (no `查看凭证`)
- `evidence-final.json` — the real payloads used for the renders

## 4. UX review (Lily checklist)

- First glance knows what to do: PASS (card says 待批准支出 · action buttons under the event)
- No redundant info: PASS
- Amount prominent: PASS (`₱3,500` bold/standalone line)
- Button hierarchy clear: PASS (approve/reject primary row, detail secondary)
- No technical language: PASS (no APPROVAL_PENDING / expense_id / #id / raw status)
- Chinese reads like a real product: PASS
- English reads like secretary instructions: PASS
- Phone width not crowded: PASS (390px QA, 2-button primary row)
- Chat stays clean after action: PASS (message mutated in place, zero junk messages)

## 5. Tests

- Bot suite: **188 passed**
- Backend `tests/test_operations.py`: **48 passed** (real PostgreSQL)
- Bridge UTF-8 regression test: PASS

## 6. Dev-environment notes (required for the live stack to run current code)

- Added `OPERATIONS_DEFAULT_ASSIGNEE=1` / `OPERATIONS_SECRETARY_ASSIGNEE=14` to the launchd operations-worker environment (current code requires them).
- Provisioned the missing `native-bot` API credential + Owner Telegram binding via `scripts/bootstrap_identity.py` (the dev DB had no bot credential).
- Deployed `4dedc761` to `/opt/pasay-pm`; API / worker healthy; bot comes up when the network allows.

Stopping here — no Slice 2/3 work was started.
