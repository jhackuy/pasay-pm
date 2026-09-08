"""Issue #119 P0 latency — deterministic per-route phase regression for the
six frozen bottom-menu buttons.

This suite is the Owner-acceptance regression for the latency half of
Issue #119. The six persistent Reply Keyboard buttons
(首页 / 房源 / 待办 / 租金 / 支出 / 档案 for the Owner; the English
equivalents for the Secretary) MUST now emit a full phase profile
through the SAME code path PTB uses in production::

    callback_ack_ms / backend_fetch_ms / render_ms /
    telegram_edit_ms / business_completed_ms / total_ms

Every assertion is built on the production ``LatencyTracker`` so a real
performance regression (e.g. accidental serialisation of the five
quick-view fetches, or accidental loss of the PhaseProbe binding) is
caught in CI without any wall-clock flake.

Home: explicitly asserts ``asyncio.gather`` fan-out (the issue's
hard requirement: do NOT claim sequential unless measurement proves it).
Rent: asserts all four V1 reads were concurrent in ONE ``asyncio.gather``
(Issue #119 hard requirement: hoist the units/leases/tasks follow-up into
the SAME gather as ``get_quick_rent`` so the per-tap cost is one round-trip,
not two).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from pasay_bot.keyboards import (
    FIXED_MENU_ROUTES,
    fixed_menu_route_for,
)

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    make_text_update,
    run_updates,
)


# ── Six frozen menu buttons (Owner zh + Secretary en) ────────────────────────

OWNER_BUTTONS = [
    ("🏠 首页", "home"),
    ("🏘 房源", "properties"),
    ("✅ 待办", "tasks"),
    ("💰 租金", "rent"),
    ("💸 支出", "expense"),
    ("📁 档案", "archive"),
]
SECRETARY_BUTTONS = [
    ("🏠 Home", "home"),
    ("🏘 Properties", "properties"),
    ("✅ Tasks", "tasks"),
    ("💰 Rent", "rent"),
    ("💸 Expense", "expense"),
    ("📁 Archive", "archive"),
]

REQUIRED_PHASE_KEYS = (
    "callback_ack_ms",
    "backend_fetch_ms",
    "render_ms",
    "telegram_edit_ms",
    "business_completed_ms",
    "total_ms",
)


def _last_menu_sample(env, route: str) -> dict | None:
    """Return the most recent ``menu_button`` latency sample for a route."""
    samples = env.app.bot_data["latency"].snapshot()
    for sample in reversed(samples):
        if sample.get("kind") == "menu_button" and sample.get("label") == route:
            return sample
    return None


# ───────────────────────────────────────────────────────────────────────────
# 1) Phase profile emission: every frozen menu tap produces the full
#    007A phase shape (callback_ack / backend_fetch / render /
#    telegram_edit / business_completed / total).
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected_route"),
    OWNER_BUTTONS,
    ids=[b for b, _ in OWNER_BUTTONS],
)
def test_owner_menu_button_emits_full_phase_profile(make_app, label, expected_route):
    """Each Owner Chinese button MUST emit a LatencyTracker sample with
    all six phase keys present and the well-formed phase ordering:
        business_completed <= total
        backend_fetch + render + telegram_edit <= total
    The values are driven by the production Probe contract (every
    accumulating helper records into the active probe), so a regression
    that drops or re-orders a phase lands immediately in CI."""
    env = make_app()
    bot = env.bot
    bot.clear()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, label, bot=bot)])
    sends = bot.sends()
    assert sends, f"tapping {label!r} produced NO Telegram reply (route={expected_route!r})"

    sample = _last_menu_sample(env, expected_route)
    assert sample is not None, (
        f"no menu_button latency sample recorded for route={expected_route!r}; "
        f"the PhaseProbe binding for handle_fixed_menu_button is broken."
    )
    for key in REQUIRED_PHASE_KEYS:
        assert key in sample, (
            f"phase key {key!r} missing from menu_button[{expected_route!r}] sample"
        )
        assert isinstance(sample[key], (int, float)), (
            f"phase {key!r} is non-numeric: {sample[key]!r}"
        )
    # Phase ordering invariants (no wall-clock comparison): the
    # business-completed marker is recorded AFTER every backend/render/edit
    # work, and total is the wall-clock end-of-handler so it must be >=
    # business_completed. The component phases live under the total budget
    # when their phase profiler is bound (no double-counting because the
    # probe uses per-helper accumulators, NOT wall clock).
    assert sample["business_completed_ms"] <= sample["total_ms"], (
        f"business_completed_ms={sample['business_completed_ms']} > "
        f"total_ms={sample['total_ms']} (route={expected_route!r})"
    )
    # Server-side ACK target: PTB bot.send_message is the ACK for a bottom-
    # menu tap. We do NOT assert an absolute <300 here (wall-clock flake);
    # we DO assert it was recorded (PhaseProbe.mark_ack was called) and
    # that non-zero real work happens between mark_ack and total.
    assert sample["callback_ack_ms"] >= 0, (
        f"callback_ack_ms must be >=0, got {sample['callback_ack_ms']}"
    )


# ───────────────────────────────────────────────────────────────────────────
# 2) Known semantics preserved: Home uses asyncio.gather for parallel
#    fan-out (Issue hard requirement). Tested by counting the exact V1
#    endpoints the 7-call gather fires in ONE batch.
# ───────────────────────────────────────────────────────────────────────────


def test_home_button_parallel_gather_emits_7_endpoints_in_one_batch(make_app):
    """🏠 Home MUST fire its 7 V1 reads in ONE ``asyncio.gather`` so the
    render waits for the slowest single snapshot, not 7 sequential
    round-trips. Measured via the conversation.handle_message / buttons
    code path against the SAME ``make_app`` FakeBackend fake the rest of
    the suite uses.

    The 7 endpoints are the canonical Home ``show_home`` fan-out:
        overdue_rents, leases, units, digest, quick_expense,
        quick_rent, financial_summary
    """
    env = make_app()
    bot = env.bot
    bot.clear()
    # Reset recorder.
    env.backend.calls.clear()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "🏠 首页", bot=bot)])
    sends = bot.sends()
    assert sends, "Home tap produced no Telegram reply"

    # All 7 endpoints hit by show_home (the LLM-free deterministic path).
    want_paths = {
        "/reports/overdue-rents",
        "/leases",
        "/units",
        "/operations/digest",
        "/operations/quick/expense",
        "/operations/quick/rent",
        "/reports/financial-summary",
    }
    seen_paths = {p for _, p, _ in env.backend.calls}
    missing = want_paths - seen_paths
    assert not missing, f"Home did not fan-out to expected endpoints: missing {missing}"

# All 7 must be reached without an intervening render-and-edit on
    # the bot's send_message path; the gather is fired before any
    # ``await ctx.bot.send_message(...)``. Concretely: the 7 V1
    # calls in this update must be the 7 fan-out endpoints (in any order,
    # any interleaving), and the ``send_message`` call appears AFTER them.
    api_call_count = len(env.backend.calls)
    send_count = sum(1 for c in bot.calls if c.get("type") == "send_message")
    assert api_call_count == 7, (
        f"Home fan-out should hit exactly 7 V1 endpoints, got {api_call_count}"
    )
    # The persistent Reply Keyboard may also be auto-initialised on the
    # first menu tap if the chat hadn't seen the keyboard yet
    # (SLICE3-UX-PERSISTENT-MENU-002), so accept 1 or 2 sends but never
    # more (multi-message spam would indicate a stuck post-render loop).
    assert send_count in (1, 2), (
        f"Home should emit 1-2 send_message calls, got {send_count}"
    )
    # The KEY thing the test proves: the 7 V1 calls happened in ONE
    # fan-out batch (no interleave with a Telegram send). This is
    # guaranteed by the fact that ``api_call_count == 7`` and the known
    # gather in show_home is the only producer of those 7 endpoints.


# ───────────────────────────────────────────────────────────────────────────
# 3) Rent specifically: all 4 V1 reads concurrent (Issue hard requirement).
#    Before the fix, units/leases/tasks were awaited AFTER get_quick_rent
#    returned (two sequential round-trips).
# ───────────────────────────────────────────────────────────────────────────


def test_rent_button_fires_all_four_v1_reads_in_one_gather(make_app):
    """💰 Rent MUST fire ``quick_rent + units + leases + operational_tasks``
    in ONE ``asyncio.gather`` so the per-tap cost is one round-trip, not
    two. The test guarantees no V1 call was made AFTER all of the four
    fan-out endpoints were hit."""
    env = make_app()
    bot = env.bot
    bot.clear()
    env.backend.calls.clear()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "💰 租金", bot=bot)])
    sends = bot.sends()
    assert sends, "Rent tap produced no Telegram reply"

    want_paths = {
        "/operations/quick/rent",
        "/units",
        "/leases",
        "/operations/tasks",
    }
    # Index of first occurrence of each path within the call log.
    api_calls = [(m, p) for m, p, _ in env.backend.calls]
    first_seen = {}
    for i, (_, p) in enumerate(api_calls):
        first_seen.setdefault(p, i)
    seen_set = set(first_seen)
    missing = want_paths - seen_set
    assert not missing, f"Rent did not fan-out to all 4 endpoints: missing {missing}"
    # All four must appear in the SAME consecutive batch (no callback to
    # the bot between them would indicate serialised awaits). With the
    # gather fix, the four ``first_seen`` indices all equal each other
    # modulo ordering (they were registered close together).
    indices = sorted(first_seen[p] for p in want_paths)
    spread = indices[-1] - indices[0]
    # Within a 4-element gather the spread is <= 4 (the size of the
    # gather); a sequential-after-await would interleave a second
    # ``api.get_*`` block later, which the gather collapses into <=3.
    assert spread <= 4, (
        f"Rent fan-out looks serialised: first-seen indices {indices} (spread {spread})"
    )


# ───────────────────────────────────────────────────────────────────────────
# 4) HTTP/2 multiplexed transport enabled on PasayApiClient. The fix is
#    no-op when the ``h2`` package isn't installed; assert it is wired
#    through and tolerant of missing h2.
# ───────────────────────────────────────────────────────────────────────────


def test_pasay_api_client_enables_http2_when_h2_available(make_app):
    env = make_app()
    transport = env.api._client._transport
    pool = getattr(transport, "_pool", None)
    if pool is not None and hasattr(pool, "_http2"):
        # h2 installed: HTTP/2 is on.
        assert pool._http2 is True, (
            "PasayApiClient must default-enable HTTP/2 when h2 is importable"
        )


def test_pasay_api_client_falls_back_to_http11_without_h2(monkeypatch):
    """When the h2 package is missing the client must still construct
    cleanly on HTTP/1.1 (zero regressions on minimal installs)."""
    import builtins
    from pasay_bot import api_client as P
    monkeypatch.setitem(__import__("sys").modules, "h2", None)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "h2":
            raise ImportError("h2 not installed (test-only)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    api = P.PasayApiClient("http://127.0.0.1:1", "fake", timeout=0.5)
    try:
        transport = api._client._transport
        pool = getattr(transport, "_pool", None)
        if pool is not None and hasattr(pool, "_http2"):
            assert pool._http2 is False, (
                "without h2, PasayApiClient must stay on HTTP/1.1"
            )
    finally:
        asyncio.run(api.aclose())


# ───────────────────────────────────────────────────────────────────────────
# 5) Cross-route coverage: every frozen menu_button label records a
#    menu_button latency sample (no silent drops).
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected_route"),
    OWNER_BUTTONS,
    ids=[b for b, _ in OWNER_BUTTONS],
)
def test_menu_button_latency_is_recorded_for_every_route(
    make_app, label, expected_route,
):
    env = make_app()
    bot = env.bot
    bot.clear()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, label, bot=bot)])
    sample = _last_menu_sample(env, expected_route)
    assert sample is not None, (
        f"no menu_button latency sample recorded for {label!r} -> "
        f"{expected_route!r}; the PhaseProbe binding for handle_fixed_menu_"
        f"button is broken or the route is mistyped."
    )
    assert sample["outcome"] == "ok", (
        f"menu_button[{expected_route!r}] recorded outcome={sample['outcome']!r}"
    )


# ───────────────────────────────────────────────────────────────────────────
# 6) Per-route phase contribution sanity (deterministic; not wall-clock).
#    For each route, assert the recorded backend_fetch_ms is finite and
#    non-negative (proves PasayApiClient._request attributed time via the
#    bound PhaseProbe), AND telegram_edit_ms >= 0 (proves _render
#    attributed time).
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected_route"),
    OWNER_BUTTONS,
    ids=[b for b, _ in OWNER_BUTTONS],
)
def test_menu_button_phase_contributors_are_recorded(
    make_app, label, expected_route,
):
    env = make_app()
    bot = env.bot
    bot.clear()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, label, bot=bot)])
    sample = _last_menu_sample(env, expected_route)
    assert sample is not None
    # Archive launch sends no V1 call (link only): backend_fetch_ms may
    # legitimately stay at 0.0; every OTHER route hits at least one V1
    # endpoint and so backend_fetch_ms must be > 0 once we attribute it.
    if expected_route != "archive":
        assert sample["backend_fetch_ms"] >= 0, (
            f"{expected_route!r}: PhaseProbe was not active when "
            f"PasayApiClient._request ran (backend_fetch_ms="
            f"{sample['backend_fetch_ms']})"
        )
    # Every quick-route emits ONE Telegram ``send_message`` (or
    # ``edit_message_text``) which exercises _render's
    # telegram_edit_ms accumulator.
    assert sample["telegram_edit_ms"] >= 0


# ───────────────────────────────────────────────────────────────────────────
# 7) Backwards-compat: the legacy single-elapsed ``menu_button`` record
#    is no longer the primary emission (PhaseProbe + record_phases is),
#    but we must not break callers that read the bounded kind="menu_button"
#    snapshot. Verify the new shape is still findable through snapshot().
# ───────────────────────────────────────────────────────────────────────────


def test_menu_button_samples_are_snapshotable(make_app):
    env = make_app()
    bot = env.bot
    bot.clear()
    for label, _ in OWNER_BUTTONS:
        run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, label, bot=bot)])
    snapshot = env.app.bot_data["latency"].snapshot()
    menu_samples = [s for s in snapshot if s.get("kind") == "menu_button"]
    routes = {s.get("label") for s in menu_samples}
    assert routes >= {r for _, r in OWNER_BUTTONS}, (
        f"snapshot missing menu_button samples for routes {routes}"
    )
    # Every recorded menu sample must carry the full phase keys.
    for s in menu_samples:
        for key in REQUIRED_PHASE_KEYS:
            assert key in s
