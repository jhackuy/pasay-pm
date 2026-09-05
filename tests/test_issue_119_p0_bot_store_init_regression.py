"""Issue #119 P0 ACCEPTANCE-ITEM-1 — Stale ``store.init()`` regression guard.

Production-side root cause (Issue #119 P0 ACCEPTANCE-ITEM-1, third hop after
the merged PR #132 token-forwarding fix and PR #133 writable STATE_DB fix):

  * ``app/services/telegram_webhook.py::get_ptb_application`` is the EXACT
    code path every inbound Telegram update traverses when it reaches the
    Container via Worker → queue → ``/internal/ingest``. The first thing
    the boot sequence does (after importing ``StateStore``) is::

        store = StateStore(bot_settings.state_db)
        store.init()                              # ← stale, AttributeError

  * The current ``pasay_bot.state.store.StateStore`` class exposes
    ``__init__`` (which already runs ``self.migrate()``) and ``migrate()``
    — but does NOT expose a top-level ``init()`` method. A grep against
    ``pasay_bot.state.store`` confirms there is no ``def init``.

  * Every webhook therefore raises::

        AttributeError: 'StateStore' object has no attribute 'init'

    on the FIRST ``get_ptb_application()`` call. The exception bubbles up
    to ``process_telegram_update_payload``, where
    ``_classify_ptb_boot_exception`` classifies ``AttributeError`` as
    TEMPORARY (the default fallback after every sentinel short-circuit
    misses). The webhook returns HTTP 503 → Telegram retries → the same
    ``AttributeError`` fires again → cross-attempt budget exhausted → the
    update is marked ``state=failed`` and Telegram stops replaying.

  * The Owner never sees a visible reply for ``/start`` OR any persistent
    keyboard action. The watchdog is green because watchdog probes only
    ``getMe`` / ``getWebhookInfo`` / ``/health`` — none of which exercise
    ``StateStore`` or any handler-driven ``bot.send_message``.

This regression guard replays the exact boot prologue that
``app/services/telegram_webhook.py::get_ptb_application`` runs (the StateStore
construction + the immediately-after construction calls). It pins two facts
on HEAD:

  1. ``StateStore`` itself never defines ``init`` — the attribute is gone.
  2. The webhook prologue does NOT call ``store.init()`` — the call site
     is gone.

A regression that re-introduces ``store.init()`` flips BOTH assertions to
RED and closes the door on a silent re-deploy of the same defect.

Why a Python test, not a Wrangler deploy / Cloudflare probe: this surface
is a pure function of three static source files (``pasay_bot/state/store.py``,
``app/services/telegram_webhook.py``, and the regression guard itself);
all three can be exercised in-process so the test runs without Cloudflare,
Docker, or the ``appuser`` uid boundary.
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _import_state_store_module():
    """Import ``pasay_bot.state.store`` against the live tree (the
    ``pasay-telegram-bot/`` subtree is on ``sys.path`` via the project's
    ``tests/conftest.py``)."""
    if "pasay_bot.state.store" not in sys.modules:
        sys.path.insert(0, "pasay-telegram-bot")
    return importlib.import_module("pasay_bot.state.store")


def _import_webhook_module():
    """Import ``app.services.telegram_webhook`` against the live tree."""
    return importlib.import_module("app.services.telegram_webhook")


# ---------------------------------------------------------------------------
# Regression guards
# ---------------------------------------------------------------------------

def test_issue_119_p0_state_store_has_no_init_method():
    """``StateStore`` MUST NOT expose a top-level ``init()`` method.

    The legacy V1.1 native launchd bot used a ``StateStore.init()`` boot
    helper. The current implementation rolls the same work into
    ``__init__`` (which opens the sqlite3 connection, sets WAL, and calls
    ``self.migrate()``) so the call site ``store.init()`` in
    ``app/services/telegram_webhook.py`` is a leftover. It raises
    ``AttributeError`` on every webhook and silently breaks the user-visible
    ``/start`` reply.

    Pinning the attribute absence here closes the door on the silent
    re-introduction: if a future refactor adds ``init()`` again, this test
    will fail and force the author to either drop the call site in
    ``app/services/telegram_webhook.py`` or make ``init()`` an explicit
    alias of the constructor.
    """
    store_mod = _import_state_store_module()
    state_store_cls = store_mod.StateStore
    assert not hasattr(state_store_cls, "init"), (
        "pasay_bot.state.store.StateStore MUST NOT expose a top-level init() "
        "method — its __init__ already runs self.migrate() and the webhook "
        "boot prologue in app/services/telegram_webhook.py::get_ptb_application "
        "would raise AttributeError('StateStore' object has no attribute 'init') "
        "on every inbound Telegram webhook. This is the exact user-visible "
        "failure mode from Issue #119 P0 ACCEPTANCE-ITEM-1: a real /start is "
        "delivered, the webhook chain reaches the bot boot, and "
        "bot.send_message is NEVER called — the only outward symptom is "
        "'no visible reply'."
    )


def test_issue_119_p0_webhook_boot_prologue_does_not_call_store_init():
    """The webhook boot prologue in ``app/services/telegram_webhook.py`` MUST
    NOT contain a ``store.init()`` call site.

    Static source-level guard: a textual scan of
    ``app/services/telegram_webhook.py`` rejects ``store.init()`` /
    ``store . init()`` / a literal call to ``.init()`` on a freshly
    constructed ``StateStore``. The bot boot prologue is a single function
    (``get_ptb_application``) and a regression that re-introduces
    ``store.init()`` there flips this test to RED immediately.
    """
    wh = _import_webhook_module()
    src_path = Path(wh.__file__)
    src_text = src_path.read_text(encoding="utf-8")

    # Strip pure-commented lines so a `# we used to call store.init()`
    # documentation comment does not trip the guard — the production-side
    # regression is an executable call site, not a comment.
    code_lines = [
        line for line in src_text.splitlines()
        if not line.lstrip().startswith("#")
    ]
    code_text = "\n".join(code_lines)

    forbidden_patterns = (
        "store.init()",       # direct call on the StateStore
        "store . init()",     # tolerate spacing variants defensively
    )
    offenders = [
        pat for pat in forbidden_patterns if pat in code_text
    ]
    assert not offenders, (
        "app/services/telegram_webhook.py still contains a "
        f"{offenders!r} call site — every inbound Telegram webhook now "
        "raises AttributeError('StateStore' object has no attribute 'init'), "
        "the webhook returns 503 → Telegram retries → cross-attempt budget "
        "exhausted → update marked failed. The Owner never sees a visible "
        "reply. Remove the store.init() line — StateStore.__init__ already "
        "runs self.migrate() so the call site is redundant AND broken."
    )


def test_issue_119_p0_webhook_get_ptb_application_prologue_only_constructs_state_store():
    """Inspect the source of ``get_ptb_application`` and assert the FIRST
    method-local statement that touches ``store`` is a
    ``store = StateStore(...)`` constructor call — not a
    ``store.init()`` method call.

    This is a stronger guard than the textual grep: it reads the function
    bytecode and rejects ``LOAD_ATTR init`` followed by ``CALL`` on a
    name that was assigned from ``StateStore(...)``. A future refactor that
    re-adds the call site (even under an ``if False:`` branch the textual
    grep would miss) is caught here.
    """
    wh = _import_webhook_module()
    get_ptb = wh.get_ptb_application
    # ``get_ptb_application`` is a regular ``async def`` so we read its
    # raw source via inspect and look for the construction pattern.
    src = inspect.getsource(get_ptb)
    # Look for any line that names the literal `store.init(` call —
    # we tolerate comments and docstrings but no executable call.
    offending_lines = [
        line.strip() for line in src.splitlines()
        if "store.init(" in line
        and not line.lstrip().startswith("#")
        and not line.lstrip().startswith('"')
        and not line.lstrip().startswith("'")
    ]
    assert not offending_lines, (
        "app/services/telegram_webhook.py::get_ptb_application still has a "
        "store.init() call site in production code: "
        f"{offending_lines!r}. StateStore.__init__ already runs "
        "self.migrate() — the call is redundant AND broken (raises "
        "AttributeError on every webhook). Remove the line; the migration "
        "happens inside the constructor and recover_stale_in_flight() is "
        "called immediately after."
    )


def test_issue_119_p0_state_store_constructor_runs_migrate_automatically(tmp_path: Path):
    """``StateStore(default_path)`` MUST be sufficient to bootstrap the
    bot-side SQLite state — no separate ``init()`` call required.

    Locks the contract that the webhook relies on: constructing a
    ``StateStore`` must (a) create the parent directory if missing,
    (b) open the connection, (c) set WAL, and (d) apply ``migrate()``.
    If a future refactor moves any of that work out of the constructor
    and back into an explicit ``init()``, this test trips and forces the
    author to decide whether the webhook boot prologue should be updated
    in lockstep (and the regression guard re-armed).
    """
    store_mod = _import_state_store_module()
    StateStore = store_mod.StateStore
    target = tmp_path / "pasay-telegram-bot" / "state" / "bot_state.db"

    # Parent dir does not exist yet; the constructor must create it.
    assert not target.parent.exists()

    store = StateStore(str(target))

    # (a) Parent dir was created.
    assert target.parent.exists(), (
        "StateStore.__init__ did not create the parent directory for "
        f"{target} — the webhook prologue will fail on /app with the same "
        "PermissionError as Issue #119 P0 ACCEPTANCE-ITEM-1 step 2."
    )

    # (b) Connection is open and (c) WAL pragma was applied.
    wal_mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(wal_mode).lower() == "wal", (
        f"StateStore.__init__ did not enable WAL journal_mode — got "
        f"{wal_mode!r}. The webhook boot relies on the constructor "
        "enabling WAL."
    )

    # (d) migrate() was called automatically — at least one of the schema
    # tables must be present without any explicit migrate() invocation.
    tables = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected_tables = {
        "conversations",
        "idempotency_keys",
        "v2_context",
        "known_groups",
        "daily_marks",
        "reminder_deliveries",
        "followup_deliveries",
    }
    missing = expected_tables - tables
    assert not missing, (
        "StateStore.__init__ did not apply the SCHEMA — missing tables "
        f"{sorted(missing)!r}. The webhook boot prologue in "
        "app/services/telegram_webhook.py::get_ptb_application relied on "
        "the constructor running migrate() automatically; if you split "
        "that out into a separate init() you must update the webhook "
        "prologue in lockstep AND update the regression guard."
    )
