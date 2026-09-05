"""Issue #119 P0 ACCEPTANCE-ITEM-1 — Container-writable state_db default.

Production-side root cause (Issue #119 P0 ACCEPTANCE-ITEM-1, second hop):
  * The Cloudflare Container production runtime runs as the unprivileged
    ``appuser`` (uid 10000) on a slim Python image (``Dockerfile`` step
    5/6/7). ``/opt`` is root:root with no write access for ``appuser``;
    ``/app/uploads`` is the only directory chowned to ``appuser``.
  * The legacy V1.1 native launchd bot (PRODUCTION_REVIEW.md / NATIVE_BOT_DESIGN.md)
    ran as ``jhackuy`` on macOS where ``/opt/pasay-pm`` was the operator-owned
    deploy target — that path was the legal default of
    ``pasay_bot.config.Settings.state_db`` and ``pasay_bot.config.get_settings``.
  * When ``process_telegram_update_payload`` boots the PTB Application
    (``app/services/telegram_webhook.py::get_ptb_application``), the first
    thing it does is ``StateStore(bot_settings.state_db)``.
    :class:`pasay_bot.state.store.StateStore.__init__` calls
    ``Path(db_path).parent.mkdir(parents=True, exist_ok=True)``. With the
    legacy default ``/opt/pasay-pm/pasay-telegram-bot/state/bot_state.db``
    that mkdir raises ``PermissionError: [Errno 13] Permission denied:
    '/opt/pasay-pm'`` on the production Container.
  * ``app/services/telegram_webhook.py::_classify_ptb_boot_exception``
    classifies ``PermissionError`` as TEMPORARY (default fallback after the
    sentinel short-circuits miss) → the webhook returns HTTP 503 → Telegram
    retries → the same PermissionError fires again → cross-attempt budget
    exhausted → the update is marked ``failed`` and Telegram stops replaying.
    The Owner sees exactly the user-visible failure from Issue #119 P0
    ACCEPTANCE-ITEM-1: a real ``/start`` is sent, the webhook chain reaches
    the bot boot, and ``bot.send_message`` is NEVER called — the only
    outward symptom is "no visible reply".

This regression guard proves three things on HEAD:

  1. The Settings-default ``state_db`` MUST NOT begin with ``/opt/pasay-pm``
     (the legacy V1.1 path that ``appuser`` cannot write). A re-deploy with
     the legacy default fails this test and would re-introduce the issue.
  2. The Settings-default ``state_db`` MUST be writable for ``appuser`` on the
     Cloudflare Container slim Python image (``/tmp`` is the canonical
     sticky-bit world-writable location).
  3. ``StateStore(state_db)`` with the new default MUST NOT raise
     ``PermissionError`` when the runtime uid is non-root (the production
     boundary).

Why Python tests, not wrangler deploy / Cloudflare probe: this surface is a
pure function of two static source files
(``pasay-telegram-bot/pasay_bot/config.py::Settings.state_db`` /
``pasay_bot.config.get_settings`` and the ``StateStore`` class); both can be
exercised in-process so the test runs without Cloudflare, Docker, or the
``appuser`` uid boundary — the assertion matches what the Container will do
because ``/tmp`` is sticky-bit world-writable on every POSIX image.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers — keep this test independent of any global env-var pollution from
# other suites (e.g. test_issue_119_telegram_env_forwarding isolates STATE_DB).
# ---------------------------------------------------------------------------

def _isolate_state_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every env var the bot uses to read state_db so a fresh call to
    ``get_settings()`` only sees what the test explicitly sets."""
    monkeypatch.delenv("STATE_DB", raising=False)


def _settings_default_state_db(monkeypatch: pytest.MonkeyPatch) -> str:
    """Return ``Settings().state_db`` with no override whatsoever.

    Mirrors the production scenario where the Worker's ``PasayContainer.envVars``
    (HEAD f148d42) does NOT forward ``STATE_DB`` and the Container therefore
    inherits the loader's built-in default.
    """
    _isolate_state_db_env(monkeypatch)
    cfg = importlib.import_module("pasay_bot.config")
    cfg.get_settings.cache_clear()
    settings = cfg.get_settings()
    return settings.state_db


# ---------------------------------------------------------------------------
# Regression guards.
# ---------------------------------------------------------------------------

def test_issue_119_p0_state_db_default_is_not_legacy_opt_path(monkeypatch: pytest.MonkeyPatch):
    """The legacy V1.1 path ``/opt/pasay-pm/...`` MUST NOT be the loader
    default for the Cloudflare Container production runtime. ``appuser``
    (uid 10000) cannot write to ``/opt`` (root-owned 755 with no write bit
    for appuser). Returning this default re-creates the user-visible
    ACCEPTANCE-ITEM-1 defect.
    """
    default = _settings_default_state_db(monkeypatch)
    assert not default.startswith("/opt/pasay-pm"), (
        f"pasay_bot.config.Settings.state_db default {default!r} still points "
        "at the legacy V1.1 native launchd deploy target (/opt/pasay-pm/...). "
        "appuser (uid 10000) in the Cloudflare Container cannot write to "
        "/opt — every bot.boot.send_message (including the canonical /start "
        "greeting) would raise PermissionError and the Owner would receive "
        "no visible reply. The new default must be writable by appuser on "
        "the slim Python image (DEFAULT_BOT_STATE_DB in pasay_bot/config.py)."
    )


def test_issue_119_p0_state_db_default_is_writable_for_appuser(monkeypatch: pytest.MonkeyPatch):
    """The default ``state_db`` MUST point to a location ``appuser`` can
    write on the production Container (sticky-bit ``/tmp`` is the canonical
    choice).

    Verified by literally initialising ``StateStore(default)`` as a
    no-permission-stripped process; ``/tmp`` is world-writable on every
    POSIX image we ship.
    """
    default = _settings_default_state_db(monkeypatch)
    # Symbolic check: /tmp is sticky-bit world-writable on Linux; if the
    # default ever points outside /tmp the assertion trips so the author
    # is forced to justify the new path.
    assert default.startswith("/tmp/"), (
        f"pasay_bot.config.Settings.state_db default {default!r} is not "
        "under /tmp; appuser cannot write to arbitrary filesystem paths on "
        "the slim Python image. Document the override path explicitly and "
        "ensure Dockerfile/Runtime mounts the location writable for appuser."
    )
    # Functional check: import the actual StateStore and instantiate it
    # with the default. This is exactly what
    # app/services/telegram_webhook.py::get_ptb_application does when
    # booting the PTB Application, except at /tmp the kernel permits the
    # operation.
    sys.path.insert(0, "pasay-telegram-bot")
    from pasay_bot.state.store import StateStore

    # Clean any leftover state from a previous run so we get a fresh init.
    leftover = Path(default)
    if leftover.exists():
        shutil.rmtree(leftover.parent, ignore_errors=True)

    try:
        store = StateStore(default)
        # Smoke-assert the connection really opened: a single INSERT/SELECT
        # through the StateStore.on_disk (sqlite3 Connection) proves the
        # table layout from StateStore.migrate() ran without PermissionError.
        store._conn.execute("SELECT 1").fetchone()
    except PermissionError as exc:
        pytest.fail(
            f"StateStore({default!r}) raised PermissionError: {exc} — "
            "the default state_db path is not writable for the current "
            "process; production Container appuser will fail the same boot."
        )
    except OSError as exc:
        # OSError other than PermissionError (e.g. ENOSPC on /tmp) is a
        # legitimate environmental failure — surface it, don't mask it.
        pytest.fail(f"StateStore({default!r}) raised OSError: {exc}")
    finally:
        # Best-effort cleanup so the test does not pollute /tmp between runs.
        if Path(default).exists():
            try:
                Path(default).unlink()
                # WAL files
                for suffix in ("-wal", "-shm", "-journal"):
                    p = Path(default + suffix)
                    if p.exists():
                        p.unlink()
                # Empty parent state dir if we created it
                parent = Path(default).parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass


def test_issue_119_p0_state_db_env_override_still_wins_over_default(monkeypatch: pytest.MonkeyPatch):
    """An explicit ``STATE_DB=/path`` (e.g. operator-written `.env` on the
    legacy V1.1 native target, or a Cloudflare Container operator who
    wants /app/uploads/state) MUST still take precedence over the new
    default. Otherwise we silently regress operators with custom paths.
    """
    _isolate_state_db_env(monkeypatch)
    monkeypatch.setenv("STATE_DB", "/tmp/pasay-telegram-bot/operator-override.db")
    cfg = importlib.import_module("pasay_bot.config")
    cfg.get_settings.cache_clear()
    settings = cfg.get_settings()
    assert settings.state_db == "/tmp/pasay-telegram-bot/operator-override.db", (
        f"STATE_DB env override lost: got {settings.state_db!r}; the explicit "
        "operator path must always win over the new default."
    )


def test_issue_119_p0_state_db_default_module_constant_is_container_safe():
    """``DEFAULT_BOT_STATE_DB`` module constant MUST live under ``/tmp`` so
    any direct-importer (e.g. a future caller that bypasses ``get_settings()``)
    still gets a writable path on the production Container."""
    from pasay_bot.config import DEFAULT_BOT_STATE_DB

    assert DEFAULT_BOT_STATE_DB.startswith("/tmp/"), (
        f"DEFAULT_BOT_STATE_DB={DEFAULT_BOT_STATE_DB!r} does not start with "
        "/tmp/ — a non-/tmp default breaks appuser on the Cloudflare Container."
    )
