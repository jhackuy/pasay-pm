# V1.2.2 Phase C1.1 — FAST UX / Latency Gate Engineering Brief (Codex Max)

> Author: Hermes (orchestrator) · Principal engineer: Codex Max · Date: 2026-08-11
> Context: C1 correctness/security ACCEPTED. The one blocking issue is TODAY's
> 15–45s Telegram latency (LLM reasoning model on the critical path). **Do NOT
> enter C2** (no action execution / no financial write / no new autopilot).
> Hermes owns Telegram UX, E2E latency measurement, model latency comparison, and
> deploy. You own the deterministic-first TODAY backend, enrichment endpoints,
> latency instrumentation, and tests.

## 0. Hard constraints (same as C1, reiterated)
1. **READ-ONLY.** No operational mutation, no financial write, no unauthorized
   entity, no model-generated factual amount/date leakage. The only DB write in
   the whole surface stays the optional `copilot_runs` audit row.
2. Do NOT change A+B.1 or C1's committed contracts unless a regression proves a real bug.
3. **Do NOT make the whole product wait on the LLM.** TODAY's first screen must
   never block on a provider timeout. LLM is enrichment only.
4. KISS. If the deterministic TODAY is fast (<1s), do NOT add Redis/caching. Probe
   first (§5). No distributed cache for a ~10-property portfolio.
5. Provider routing is centralized config — no scattered model-specific `if`.

## 1. Where we are (verified by Hermes)
- Deterministic priority engine **already exists and is production-grade**:
  `app/services/copilot/ranking.py` — tiered policy (severe overdue 4000 > overdue
  3000 > lease-expiring 2000 > maintenance 1000 > low 100), structured refinements
  capped per dimension, stable tie-breaks, documented, pure of text length.
  `RankedItem` carries `label` + `reason` (deterministic).
- **Problem:** `app/services/copilot/today.py::build_today()` builds the context +
  ranks (`rank_items`) instantly, then **unconditionally calls the LLM**
  (`client.complete`, max_tokens 8000, reasoning model 15–45s) and returns only
  after the LLM+post-validation. So TODAY's critical path = the LLM latency.
- Backend regression baseline: `pytest tests/ -q -o addopts="-m 'not eval'"` → **228 passed**, 2 deselected.
- Bot baseline: `pasay-telegram-bot/tests/ -q` → **147 passed**.

## 2. Deliverable A — Deterministic-First TODAY (no LLM on critical path)
New `app/services/copilot/today_fast.py` (keep today.py intact for now OR refactor
it to delegate — your call, but keep the committed TODAY schema if the router/UX
depend on it; better: make the fast path the DEFAULT `/today` response and move the
old LLM path into the WHY/enrichment layer).

`build_today_deterministic(db, user, *, now=None) -> TodayFastResult`:
- T0: build context (reuse `build_copilot_context`), T1: `rank_items(context)`.
- Take top-3 → each `TodayItem{item_ref, reason_why_important=RankedItem.reason,
  suggested_action=default_action(kind)}` (reuse today.py helpers `_fallback_item`
  / `_default_action` — move to a shared module).
- **Deterministic summary** (≤2 sentences): built from the top items, e.g.
  "3 项超高优先级：2 项严重逾期租金（合计 ₱661,000），1 项租约 7 天内到期。"
  — a pure function of the ranked items (no LLM). Versioned format.
- **NO provider call, NO timeout, NO failure path.** Returns in ms.
- Include a `flags` / `enriched:bool=False` marker.
- Latency instrumentation (Deliverable D).

Router change: `POST /operations/copilot/today` returns the **deterministic** brief
by default (fast). This is the immediate Telegram response.

## 3. Deliverable B — WHY enrichment (per-item, on-demand LLM)
`POST /operations/copilot/why` (RBAC admin/manager). Body `{item_ref, provider?}`.
- Ground from the context; validate `item_ref` is in the grounded set.
- Call the LLM with a scoped WHY prompt (max_tokens as needed, e.g. 2000 — short
  explain, short recommendation). Return `{item_ref, explanation, recommendation,
  grounded_refs, provider, model, latency_ms}`.
- **Fail-closed:** on provider error/timeout/malformed → return HTTP 200 with
  `{fallback:true, explanation=<deterministic RankedItem.reason>, recommendation=<default_action>}`
  so the YES/yes button still yields a useful, grounded result even when the model
  is down. (Requirement 8: WHY shows friendly fallback when provider down.)
- Post-validate explanation text: strip refs, forbid invented amounts/dates (reuse
  the C1 `_clean_human_text` + ref-denylist).

## 4. Deliverable C — Q&A enrichment (on-demand LLM)
`POST /operations/copilot/ask` (RBAC admin/manager). Body `{question, provider?}`.
- Ground to the current context + optional extra read-only query helpers for
  natural-language questions ("这个月哪个房子维修费最高？", "谁没交租？").
- **Deterministic guard:** never let the model write or return ungrounded financial
  facts. Post-validate: any amount/date it cites must be resolvable in the grounded
  context or the helpers; else strip + flag. Never execute tools.
- Returns `{answer, provider, model, latency_ms, flags}`. LLM-backed; MAY be slow
  (this is the "complex Q&A = stronger model" slot, §6).
- On provider-down → friendly deterministic fallback answer ("运营助手暂时无法联网分析，请重试或到 /overdue、/finance 查看"), never fabricated.

## 5. Deliverable D — Latency instrumentation (requirement 4)
Extend the copilot service with a structured timing breakdown returned in each
response (today/why/ask) and logged:
`context_build_ms, priority_ms, grounding_ms, llm_ms, render_ms, total_ms`.
- Measure each phase with monotonic timestamps; `llm_ms=0` when no LLM (fast TODAY).
- Do NOT just log total. Expose in the response (and a debug field the E2E can read).

## 6. Deliverable E — Provider profile map (centralized, requirement 6)
- Central config in `app/services/copilot/llm.py` (or a small `providers.py`):
  `TODAY = None (no LLM)`, `EXPLAIN = fast non-reasoning or flash`, `ASK = strong
  (deepseek-pro)`. Env-tunable. **No scattered model `if` in business code.**
- Add at least ONE fast non-reasoning provider entry for comparison (e.g.
  `deepseek-chat` — deepseek's non-reasoning chat model — same API key; or a
  configured fast flash lane). Do NOT switch defaults without eval evidence; just
  wire the option so Hermes can measure it (§F in the final report).

## 7. Deliverable F — Tests (real-PG; eval-marked only for live-LLM)
Keep the full suite green (228 backend + 147 bot, C1 adversarial/grounding, A+B.1
reminder). Add:
1. `build_today_deterministic` returns ≤3 items, top refs match `rank_items` top-K,
   **no LLM invoked** (inject a client that raises; assert NOT called).
2. Deterministic summary correctness + version.
3. `/today` endpoint returns fast even when provider is DOWN (mock/monkeypatch client
   that raises) — **no 503, no hang**, correct deterministic items. (Requirement 1/8.)
4. `/why` returns provider explanation on success; **returns deterministic fallback
   (HTTP 200) when provider raises** — no fabricated facts, refs stripped.
5. `/ask` returns answer on success; deterministic friendly fallback on provider-down;
   refuses to return ungrounded amounts (mock LLM asked to invent → stripped/flagged).
6. Latency instrumentation: fields present, monotonic, `llm_ms=0` for fast TODAY.
7. **Mutation invariant (requirement 7):** after today/why/ask calls, assert NO
   operational/financial rows changed (only optional copilot_runs audit row). Add a
   test.
8. Existing C1/adversarial/A+B.1 tests stay green.
9. `@pytest.mark.eval` smoke for why/ask (live-LLM), deselected by default.

## 8. Out of scope (do NOT touch)
- No action execution / EXECUTED transition / `executed_at` write. No C2.
- No financial write; no new table unless strictly necessary (prefer none; reuse
  `copilot_runs`).
- No Redis/distributed cache unless the probe demands it (Hermes/Max agree first).
- Do NOT modify canonical `/opt/pasay-pm/bin/start-native-api.sh` or `deploy-v12.sh`.

## 9. Delivery report (final summary to Hermes)
- Files added/changed; the deterministic TODAY architecture (C)
- Priority policy: state it's the existing `ranking.py` policy plus any new
  rule/reason fields; version string; how a low item can never displace high (B)
- WHY/Q&A contract + fallback semantics (E/G)
- Latency instrumentation shape (D)
- Provider profile map + the fast non-reasoning lane you wired (F)
- Full new-test list + `pytest tests/ -q` tail (all green)
- Explicit confirmation of the read-only / mutation invariant (I)
- Any design decision that differs + risks (K)
Do the field work: run the tests, verify the fast path actually returns without
touching the LLM, verify provider-down TODAY returns fast.
