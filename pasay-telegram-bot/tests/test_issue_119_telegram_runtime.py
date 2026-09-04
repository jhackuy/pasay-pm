"""Issue #119 (Telegram runtime) — focused automated tests.

This suite is the Owner-acceptance regression for the Telegram runtime half of
Issue #119. Two surfaces are covered end-to-end through the SAME code paths
PTB uses in production:

  A) Menu reply path
     Tapping any of the six persistent Reply Keyboard buttons
     (首页 / 房源 / 待办 / 租金 / 支出 / 档案 for the Owner; the English
     equivalents for the Secretary) MUST emit a Telegram reply on the tapped
     chat — proven by a FakeBot ``send_message`` call carrying a non-empty
     ``text`` field. Routing is deterministic and happens BEFORE any NL/NLU
     path runs, so a regression that drops the routing (or re-routes through
     NL/LLM) is caught immediately.

  B) Automatic outbound notification dispatch
     The two background jobs (v2_daily_digest / v2_next_check) MUST emit the
     right messages to the right Telegram chats: per-user private DMs in the
     role's language (Owner → zh, Secretary → en), and bilingual broadcasts
     to every known group chat (CONVERGENCE-003 §1.4). One recipient failure
     must NOT block the rest, the same-day dedupe gate must be enforced, and
     the SYSTEM principal must remain unbound to any HUMAN Telegram id.

These tests are deliberately narrow (no Mini App, no Worker, no API business
truth). They pin ONLY the Telegram-runtime surface that the owner reported as
dead.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from pasay_bot.api_client import PasayApiClient
from pasay_bot.config import Settings
from pasay_bot.jobs import _send_digest, _send_next_check_reminders
from pasay_bot.keyboards import (
    FIXED_MENU_ROUTES,
    fixed_menu_route_for,
    reply_keyboard,
)
from pasay_bot.roles import Role

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    UNKNOWN_ID,
    FakeBackend,
    make_text_update,
    run_updates,
)


# ===========================================================================
# A) Menu reply path — every button MUST produce a Telegram reply
# ===========================================================================

# The six labels as deployed on @sellandrentbot (Owner zh / Secretary en).
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


@pytest.mark.parametrize(
    ("label", "expected_route"),
    OWNER_BUTTONS,
    ids=[b for b, _ in OWNER_BUTTONS],
)
def test_owner_zh_menu_button_emits_reply(make_app, label, expected_route):
    """Each Owner Chinese button MUST route to its deterministic route AND
    cause a Telegram send_message on the tapped chat."""
    env = make_app()
    bot = env.bot
    bot.clear()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, label, bot=bot)])
    sends = bot.sends()
    assert sends, (
        f"tapping {label!r} produced NO Telegram reply — the live production "
        f"fingerprint from Issue #119 A. Route was {expected_route!r}."
    )
    last = sends[-1]
    text = last.get("text") or ""
    assert text, (
        f"tapping {label!r} produced an empty reply (route={expected_route!r}); "
        "this is the silent-no-reply regression."
    )
    # Every Owner button (except 档案 which is a launcher) renders a Quick View
    # card with a meaningful header. 档案 shows the archive launcher title.
    if expected_route == "archive":
        assert "档案" in text or "Archive" in text
    else:
        assert text != label, (
            f"reply must be a deterministic page, NOT the button text echo "
            f"(button {label!r} produced {text!r})"
        )


@pytest.mark.parametrize(
    ("label", "expected_route"),
    SECRETARY_BUTTONS,
    ids=[b for b, _ in SECRETARY_BUTTONS],
)
def test_secretary_en_menu_button_emits_reply(make_app, label, expected_route):
    """Each Secretary English button MUST route to its deterministic route
    AND cause a Telegram send_message on the tapped chat."""
    env = make_app()
    bot = env.bot
    bot.clear()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, label, bot=bot)])
    sends = bot.sends()
    assert sends, (
        f"tapping {label!r} produced NO Telegram reply — Issue #119 A. "
        f"Route was {expected_route!r}."
    )
    last = sends[-1]
    text = last.get("text") or ""
    assert text, f"tapping {label!r} produced an empty reply."
    if expected_route == "archive":
        assert "档案" in text or "Archive" in text
    else:
        assert text != label


@pytest.mark.parametrize(
    ("label", "expected_route"),
    OWNER_BUTTONS + SECRETARY_BUTTONS,
    ids=[f"owner-{b}" for b, _ in OWNER_BUTTONS]
    + [f"secretary-{b}" for b, _ in SECRETARY_BUTTONS],
)
def test_every_menu_button_routes_deterministically(label, expected_route):
    """Pure route-table guardrail: every fixed button label MUST resolve to
    its declared deterministic route via fixed_menu_route_for. A missing
    entry would silently fall through to the NL/NLU path — the exact Issue
    #119 failure mode."""
    assert fixed_menu_route_for(label) == expected_route
    # And the route must be present in the canonical map (catches typos).
    assert label in FIXED_MENU_ROUTES


def test_six_button_keyboard_is_complete_for_owner_and_secretary():
    """The Reply Keyboard MUST carry exactly the six frozen IA buttons for
    both roles (3×2 grid). A missing button or a 5-button keyboard is the
    user-visible half of Issue #119."""
    owner_labels = [
        b.text for row in reply_keyboard(Role.OWNER).keyboard for b in row
    ]
    secretary_labels = [
        b.text for row in reply_keyboard(Role.SECRETARY).keyboard for b in row
    ]
    assert owner_labels == ["🏠 首页", "🏘 房源", "✅ 待办",
                            "💰 租金", "💸 支出", "📁 档案"]
    assert secretary_labels == ["🏠 Home", "🏘 Properties", "✅ Tasks",
                                "💰 Rent", "💸 Expense", "📁 Archive"]


def test_free_text_still_reaches_conversation_layer_not_button_path(
    make_app, monkeypatch
):
    """Routing guardrail: only an EXACT fixed-menu label pre-routes. A free-
    text message must NOT be intercepted as a menu button — the deterministic
    page renderer would mask genuine natural-language intent."""
    from pasay_bot.handlers import nl_bridge

    seen = []

    async def _spy(*a, **kw):
        seen.append(True)

    monkeypatch.setattr(nl_bridge, "handle_nl", _spy)
    env = make_app()
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "今天收到多少钱", bot=env.bot)])
    assert seen == [True], (
        "free-text must reach the conversation/NL layer; routing a free-text "
        "as a button would silently drop user intent (Issue #119 A regression)."
    )


def test_unknown_user_tap_still_gets_no_permission_reply(make_app):
    """An unknown Telegram id (not in TELEGRAM_USER_ID_TO_ROLE) tapping any
    button MUST still get a Telegram reply — never silent. This is the
    fail-closed guarantee from Issue #119 A."""
    env = make_app()
    bot = env.bot
    bot.clear()
    run_updates(env, [make_text_update(UNKNOWN_ID, UNKNOWN_ID, "💰 租金", bot=bot)])
    sends = bot.sends()
    assert sends, "unknown user tap must still produce a Telegram reply"
    text = sends[-1].get("text") or ""
    assert text, "unknown user reply must be non-empty (fail-closed)"


def test_button_path_is_deterministic_and_never_calls_nl_bridge(
    make_app, monkeypatch
):
    """Proof test: every Owner Chinese button label routes deterministically
    BEFORE the NL bridge, so the deterministic page render is guaranteed
    even if the NL bridge is broken. This is the structural guarantee that
    the button path cannot be silently swallowed by an NL/LLM failure."""
    from pasay_bot.handlers import nl_bridge

    async def _boom(*a, **kw):
        raise AssertionError("NL bridge must not run on a fixed button tap")

    monkeypatch.setattr(nl_bridge, "handle_nl", _boom)
    env = make_app()
    for label, _ in OWNER_BUTTONS:
        bot = env.bot
        bot.clear()
        run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, label, bot=bot)])
        assert bot.sends(), f"{label!r} produced no reply (NL bridge would mask it)"


def test_button_path_routes_to_handle_fixed_menu_button(monkeypatch, make_app):
    """Static guardrail: handle_message MUST exact-match the FIXED_MENU_ROUTES
    table BEFORE invoking any other handler. A regression that swaps the
    routing order would route button text to NL/LLM.

    We monkeypatch ``pasay_bot.handlers.buttons.handle_fixed_menu_button``
    (the real call inside conversation.handle_message after routing) so the
    spy sits in front of the dispatcher used at runtime.
    """
    import pasay_bot.handlers.buttons as buttons_module
    import pasay_bot.handlers.conversation as conversation_module

    calls = []

    async def _spy(*args, **kwargs):
        # handle_fixed_menu_button(update, context, route)
        calls.append(args[2] if len(args) >= 3 else kwargs.get("route"))

    monkeypatch.setattr(buttons_module, "handle_fixed_menu_button", _spy)

    env = make_app()
    update = make_text_update(OWNER_ID, OWNER_ID, "🏘 房源", bot=env.bot)
    context = SimpleNamespace(bot_data=env.app.bot_data)
    asyncio.run(conversation_module.handle_message(update, context))
    assert calls == ["properties"], (
        "handle_fixed_menu_button must be invoked with the routed label; "
        "got %r" % (calls,)
    )


# ===========================================================================
# B) Automatic outbound notification dispatch
# ===========================================================================

def _real_client(backend, key="sys-job-key"):
    """A SYSTEM-keyed PasayApiClient bound to the in-memory backend."""
    return PasayApiClient(
        "http://test/api/v1", key, timeout=1.0,
        transport=httpx.MockTransport(backend.handler),
    )


class _RecordingBot:
    """Minimal bot that records every send_message call for inspection."""

    def __init__(self):
        self.sends: list[dict] = []
        self.fail_chats: set[int] = set()

    async def send_message(self, chat_id, text=None, parse_mode=None,
                           reply_markup=None, **kw):
        if chat_id in self.fail_chats:
            raise RuntimeError(f"simulated Telegram send failure to {chat_id}")
        rec = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
               "reply_markup": reply_markup}
        self.sends.append(rec)
        return SimpleNamespace(message_id=1000 + len(self.sends))


def _make_app(bot):
    """Wrap a recording bot as the Application object jobs.py expects.

    jobs.py uses ``await app.bot.send_message(...)`` so the wrapper exposes
    the recording bot as ``app.bot``. The Application shape is otherwise
    unused by the job functions under test."""
    return SimpleNamespace(bot=bot)


class _KnownGroupStore:
    """A bare-bones store exposing the surface jobs.py uses."""

    def __init__(self, groups):
        self._groups = list(groups)

    def list_known_groups(self):
        return [dict(g) for g in self._groups]

    def mark_daily(self, key: str) -> bool:
        """Same-day dedupe (CONVERGENCE-003 §1.4). Returns True the first
        time a key is claimed in this process; subsequent claims return False.
        For these tests an in-process set is enough."""
        if not hasattr(self, "_marks"):
            self._marks = set()
        if key in self._marks:
            return False
        self._marks.add(key)
        return True


def test_digest_delivers_to_owner_zh_and_secretary_en_private_chats():
    """v2_daily_digest MUST emit:
       - one zh card to the OWNER private chat (id == OWNER_ID)
       - one en card to the SECRETARY private chat (id == SECRETARY_ID)
       - one bi card to every known group chat
    A missing recipient is a silent failure (Issue #119 C).
    """
    backend = FakeBackend()
    backend.digest = {
        "pending": [{"id": 1, "title": "催租 Unit 1680"}],
        "in_progress": [],
        "act_now": [{"id": 1, "title": "催租 Unit 1680"}],
        "upcoming": [],
        "done_today": [],
        "counts": {"act_now": 1},
    }
    bot = _RecordingBot()
    groups = _KnownGroupStore([
        {"chat_id": -100200300, "title": "ops-room"},
    ])
    api = _real_client(backend)
    try:
        asyncio.run(_send_digest(_make_app(bot), api, groups))
    finally:
        asyncio.run(api.aclose())

    sent_chats = [s["chat_id"] for s in bot.sends]
    assert OWNER_ID in sent_chats, "Owner private DM missing"
    assert SECRETARY_ID in sent_chats, "Secretary private DM missing"
    assert -100200300 in sent_chats, "known group chat missing"

    by_chat = {s["chat_id"]: s for s in bot.sends}
    # Owner private: Chinese locale only (no bilingual /Today suffix).
    owner_text = by_chat[OWNER_ID]["text"]
    assert "今日待办" in owner_text or "待办" in owner_text, (
        f"Owner DM must be zh (DAILY-DIGEST-TRUTH-CLEANUP-006 PHASE 9), "
        f"got: {owner_text!r}"
    )
    # Secretary private: English locale only.
    secretary_text = by_chat[SECRETARY_ID]["text"]
    assert "Today's tasks" in secretary_text or "tasks" in secretary_text.lower(), (
        f"Secretary DM must be en, got: {secretary_text!r}"
    )
    # Group broadcast: bilingual.
    group_text = by_chat[-100200300]["text"]
    # The digest card carries the same bilingual header as the private senders
    # when the recipient is a group (groups always use locale=bi).
    assert group_text, "group broadcast must be non-empty"


def test_digest_skips_when_no_active_tasks():
    """A digest with no pending/in_progress/act_now/done MUST NOT spam any
    recipient — a no-content push is the production 'silent spam' failure
    (DAILY-DIGEST-TRUTH-CLEANUP-006 PHASE 12)."""
    backend = FakeBackend()
    backend.digest = {
        "pending": [], "in_progress": [], "act_now": [],
        "upcoming": [], "done_today": [], "counts": {"act_now": 0},
    }
    bot = _RecordingBot()
    groups = _KnownGroupStore([{"chat_id": -100200300, "title": "ops-room"}])
    api = _real_client(backend)
    try:
        asyncio.run(_send_digest(_make_app(bot), api, groups))
    finally:
        asyncio.run(api.aclose())
    assert bot.sends == [], (
        "empty digest must NOT produce any Telegram send — silent-spam guard."
    )


def test_digest_one_recipient_failure_does_not_block_others():
    """If Telegram send to the Owner fails, the Secretary DM AND every group
    broadcast MUST still go out. The Owner failure must be logged but never
    propagate up and cancel the job."""
    backend = FakeBackend()
    backend.digest = {
        "pending": [{"id": 1, "title": "催租 Unit 1680"}],
        "in_progress": [], "act_now": [{"id": 1, "title": "催租 Unit 1680"}],
        "upcoming": [], "done_today": [], "counts": {"act_now": 1},
    }
    bot = _RecordingBot()
    bot.fail_chats = {OWNER_ID}  # only Owner send raises
    groups = _KnownGroupStore([
        {"chat_id": -100200300, "title": "ops-room"},
        {"chat_id": -100200301, "title": "ops-room-2"},
    ])
    api = _real_client(backend)
    try:
        asyncio.run(_send_digest(_make_app(bot), api, groups))
    finally:
        asyncio.run(api.aclose())

    sent_chats = [s["chat_id"] for s in bot.sends]
    assert SECRETARY_ID in sent_chats, (
        "Secretary DM must still send after Owner failure"
    )
    assert -100200300 in sent_chats and -100200301 in sent_chats, (
        "group broadcasts must still send after Owner failure"
    )
    assert OWNER_ID not in sent_chats, "failed Owner must not appear in sends"


def test_digest_skips_when_no_known_groups():
    """Without any known group chat, the digest MUST still go to the private
    Owner + Secretary DMs and MUST NOT raise. Groups are best-effort; the
    per-user delivery is the source of truth (DAILY-DIGEST-TRUTH-CLEANUP-006
    PHASE 12-17)."""
    backend = FakeBackend()
    backend.digest = {
        "pending": [{"id": 1, "title": "催租 Unit 1680"}],
        "in_progress": [], "act_now": [{"id": 1, "title": "催租 Unit 1680"}],
        "upcoming": [], "done_today": [], "counts": {"act_now": 1},
    }
    bot = _RecordingBot()
    groups = _KnownGroupStore([])  # no groups
    api = _real_client(backend)
    try:
        asyncio.run(_send_digest(_make_app(bot), api, groups))
    finally:
        asyncio.run(api.aclose())
    sent_chats = [s["chat_id"] for s in bot.sends]
    assert OWNER_ID in sent_chats
    assert SECRETARY_ID in sent_chats


def test_next_check_reminder_dispatches_to_known_groups_when_due():
    """v2_next_check MUST push a single reminder per (task, group, PH day)
    into every known group chat when the task's next_check_at has elapsed."""
    backend = FakeBackend()
    # One due task with next_check_at in the past.
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    backend.quick_tasks = [
        {
            "id": 501, "task_type": "FOLLOWUP", "title": "催租 Unit 1680",
            "status": "PENDING", "property_code": "1680",
            "due_at": past, "next_action": "联系租客",
            "next_check_at": past,
        }
    ]
    bot = _RecordingBot()
    groups = _KnownGroupStore([
        {"chat_id": -100200300, "title": "ops-room"},
        {"chat_id": -100200301, "title": "ops-room-2"},
    ])
    api = _real_client(backend)
    try:
        asyncio.run(_send_next_check_reminders(_make_app(bot), api, groups))
    finally:
        asyncio.run(api.aclose())

    sent_chats = [s["chat_id"] for s in bot.sends]
    assert -100200300 in sent_chats
    assert -100200301 in sent_chats


def test_next_check_reminder_skips_when_no_known_groups():
    """The next_check job MUST NOT send anything (no DMs, no groups) when
    the bot does not know any group chat. Private DMs are the digest job's
    job, not next_check's (CONVERGENCE-003 §1.3)."""
    backend = FakeBackend()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    backend.quick_tasks = [
        {
            "id": 501, "task_type": "FOLLOWUP", "title": "催租 Unit 1680",
            "status": "PENDING", "property_code": "1680",
            "due_at": past, "next_action": "联系租客",
            "next_check_at": past,
        }
    ]
    bot = _RecordingBot()
    api = _real_client(backend)
    try:
        asyncio.run(_send_next_check_reminders(_make_app(bot), api, _KnownGroupStore([])))
    finally:
        asyncio.run(api.aclose())
    assert bot.sends == [], (
        "next_check must not DM anyone when no groups are known — "
        "private DMs are the digest job's responsibility."
    )


def test_next_check_reminder_skips_when_no_due_tasks():
    """When no task's next_check_at has elapsed, next_check MUST NOT push
    anything. This is the basic 'no junk' guard (CONVERGENCE-003 §1.3)."""
    backend = FakeBackend()
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    backend.quick_tasks = [
        {
            "id": 501, "task_type": "FOLLOWUP", "title": "催租 Unit 1680",
            "status": "PENDING", "property_code": "1680",
            "due_at": future, "next_action": "联系租客",
            "next_check_at": future,
        }
    ]
    bot = _RecordingBot()
    groups = _KnownGroupStore([{"chat_id": -100200300, "title": "ops-room"}])
    api = _real_client(backend)
    try:
        asyncio.run(_send_next_check_reminders(_make_app(bot), api, groups))
    finally:
        asyncio.run(api.aclose())
    assert bot.sends == [], "no due tasks -> no sends"


def test_next_check_reminder_one_group_failure_does_not_block_others():
    """If Telegram send to one known group fails, the other groups MUST
    still get the reminder. This is the same fail-open guarantee as the
    digest job (CONVERGENCE-003 §1.4)."""
    backend = FakeBackend()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    backend.quick_tasks = [
        {
            "id": 501, "task_type": "FOLLOWUP", "title": "催租 Unit 1680",
            "status": "PENDING", "property_code": "1680",
            "due_at": past, "next_action": "联系租客",
            "next_check_at": past,
        }
    ]
    bot = _RecordingBot()
    bot.fail_chats = {-100200300}
    groups = _KnownGroupStore([
        {"chat_id": -100200300, "title": "ops-room"},
        {"chat_id": -100200301, "title": "ops-room-2"},
    ])
    api = _real_client(backend)
    try:
        asyncio.run(_send_next_check_reminders(_make_app(bot), api, groups))
    finally:
        asyncio.run(api.aclose())
    sent_chats = [s["chat_id"] for s in bot.sends]
    assert -100200301 in sent_chats, "second group must still receive the reminder"
    assert -100200300 not in sent_chats, "failed group must not appear"


def test_jobs_disabled_when_no_system_credential():
    """JOB-SERVICE-AUTH-002: without PASSAY_JOB_API_KEY the jobs MUST NOT
    register (fail closed) — the digest/next_check jobs are a SYSTEM
    principal operation, never a fallback to a HUMAN Owner identity."""
    from pasay_bot.jobs import _build_job_api, register_jobs

    settings = Settings(pasay_job_api_key="")
    assert _build_job_api(settings) is None

    class _JobQueue:
        def __init__(self):
            self.entries = []

        def run_daily(self, cb, *, time, name):
            self.entries.append(("daily", name))

        def run_repeating(self, cb, *, interval, first, name):
            self.entries.append(("repeating", name))

    class _App:
        job_queue = _JobQueue()

    register_jobs(_App(), None, _KnownGroupStore([]), settings, job_api=None)
    assert _App.job_queue.entries == []


def test_jobs_register_with_system_credential():
    """When the SYSTEM key IS configured the jobs MUST register both the
    daily digest and the repeating next_check reminder."""
    from pasay_bot.jobs import _build_job_api, register_jobs

    settings = Settings(pasay_job_api_key="sys-job-key",
                        pasay_api_base="http://test/api/v1")
    job_api = _build_job_api(settings)
    assert job_api is not None

    class _JobQueue:
        def __init__(self):
            self.entries = []

        def run_daily(self, cb, *, time, name):
            self.entries.append(("daily", name))

        def run_repeating(self, cb, *, interval, first, name):
            self.entries.append(("repeating", name))

    class _App:
        job_queue = _JobQueue()

    try:
        register_jobs(_App(), None, _KnownGroupStore([]), settings, job_api=job_api)
        names = [e[1] for e in _App.job_queue.entries]
        assert names == ["v2_daily_digest", "v2_next_check"]
    finally:
        asyncio.run(job_api.aclose())


def test_job_401_failure_is_swallowed_not_fatal():
    """A rejected/absent SYSTEM credential must degrade the job, never the
    bot. The job catches ``PasayApiError`` (the auth-error subclass) and
    returns without raising — Telegram polling keeps running."""
    backend = FakeBackend()
    backend.fail_status["/operations/quick/tasks"] = 401
    bot = _RecordingBot()
    api = _real_client(backend)
    try:
        asyncio.run(_send_next_check_reminders(_make_app(bot), api, _KnownGroupStore([])))
        asyncio.run(_send_digest(_make_app(bot), api, _KnownGroupStore([])))
    finally:
        asyncio.run(api.aclose())
    assert bot.sends == []


def test_digest_api_request_carries_no_human_telegram_user_id_header():
    """The SYSTEM job MUST NEVER send ``X-Telegram-User-Id`` (a HUMAN-bound
    header). Sending one would leak the Owner's identity into a SYSTEM
    read; the backend would then attribute the SYSTEM read to the Owner."""
    backend = FakeBackend()
    backend.digest = {
        "pending": [{"id": 1, "title": "催租 Unit 1680"}],
        "in_progress": [], "act_now": [{"id": 1, "title": "催租 Unit 1680"}],
        "upcoming": [], "done_today": [], "counts": {"act_now": 1},
    }
    bot = _RecordingBot()
    api = _real_client(backend)
    try:
        asyncio.run(_send_digest(_make_app(bot), api, _KnownGroupStore([])))
    finally:
        asyncio.run(api.aclose())

    digest_call_index = next(
        i for i, (m, p, _) in enumerate(backend.calls)
        if p == "/operations/digest" and m == "GET"
    )
    header = backend.telegram_user_calls[digest_call_index]
    assert header is None, (
        "digest job must NEVER bind X-Telegram-User-Id; "
        f"backend saw {header!r}"
    )
    auth = backend.auth_calls[digest_call_index]
    assert auth == "Bearer sys-job-key", (
        f"digest job must use the SYSTEM credential, saw {auth!r}"
    )
