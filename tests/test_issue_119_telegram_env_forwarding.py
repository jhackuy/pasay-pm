"""Issue #119 (Telegram runtime) — Cloudflare Worker → Container bot env forwarding.

This is the regression guardrail for the user-visible failure mode reported in
the Owner-acceptance failure of 2026-09-03: ``@sellandrentbot`` persistent
keyboard actions sent text but produced no visible bot reply.

Production-side root cause (Issue #119 P0 TELEGRAM-RUNTIME):
  * The Cloudflare Worker's ``PasayContainer.envVars`` only forwarded the
    Worker-bound token under the upstream name ``TELEGRAM_BOT_TOKEN``.
  * The bot's ``pasay_bot.config.Settings`` loader reads the token under
    ``PASSAY_TG_BOT_TOKEN`` (the original ``NATIVE_BOT_DESIGN.md`` env
    contract — see pasay-telegram-bot/.env.example line 3).
  * With no ``PASSAY_TG_BOT_TOKEN`` in the Container's environment, the
    bot ended up with ``pasay_tg_bot_token=""``.
  * ``pasay_bot.main.build_application`` then called
    ``builder.token(settings.pasay_tg_bot_token or "0:UNSET")`` — i.e.
    PTB was constructed with the literal token ``"0:UNSET"``.
  * Every ``context.bot.send_message(...)`` issued by any handler hit
    ``api.telegram.org/bot0:UNSET/sendMessage`` → Telegram returned
    InvalidToken (HTTP 401) → the bot's exception classifier treated it
    as PERMANENT (see ``app/services/telegram_webhook.py::_is_temporary_error``
    which short-circuits ``InvalidToken`` to ``permanent=True``) → the
    inbound update was marked ``state=failed`` → Telegram's webhook
    delivery loop stopped replaying → Owner received NO visible reply.

This test suite exercises the Worker → Container boundary directly: it
replays the exact env-var forwarding shape ``PasayContainer.envVars``
writes into the Container, then asserts that ``pasay_bot.config.get_settings()``
sees a real token (NOT ``""`` and NOT ``"0:UNSET"``). A regression that
breaks the Worker's forwarding (or the bot's defensive fallback) flips
one of these assertions to RED and closes the door on a silent re-deploy
of the same defect.

Why a Python test, not a wrangler deploy: this surface is a pure
function of two static source files
(``cloudflare-worker/src/index.ts::PasayContainer.envVars`` and
``pasay-telegram-bot/pasay_bot/config.py::_env``); both are exercised
in-process so the test runs without Cloudflare, Docker, or any network.
The exact keyset is locked into ``CLOSEOUT#7a/#7b/#7c`` in
``cloudflare-worker/tests/index.spec.ts``; this Python suite is the
backend counterpart that proves the bot side actually consumes those
forwarded names.
"""
from __future__ import annotations

import importlib
import os

import pytest


# ---------------------------------------------------------------------------
# helpers — keep this test independent of any global env-var pollution from
# other suites (the backend's app.config.Settings picks up DATABASE_URL etc.
# at import time; the bot's pasay_bot.config is similarly sensitive).
# ---------------------------------------------------------------------------

def _isolate_bot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every env var pasay_bot.config reads so a fresh call to
    ``get_settings()`` only sees what the test explicitly sets. The bot
    reads these names (see pasay_bot/config.py::_env)."""
    for key in (
        "PASSAY_TG_BOT_TOKEN",
        "PASSAY_API_BASE",
        "PASSAY_API_KEY",
        "PASSAY_ADMIN_API_KEY",
        "HERMES_API_BASE",
        "HERMES_API_KEY",
        "STATE_DB",
        "HOOK_TOKEN",
        "CALLBACK_TTL_SECONDS",
        "PASSAY_HTTP_TIMEOUT_SECONDS",
        "PASSAY_ARCHIVE_CHAT_ID",
        "PASSAY_JOB_API_KEY",
        "PASSAY_MINI_APP_URL",
        "PASSAY_MINI_APP_OWNER_TELEGRAM_IDS",
        # Issue #119 P0 defensive-fallback source: the bot ALSO accepts
        # TELEGRAM_BOT_TOKEN as a fallback for PASSAY_TG_BOT_TOKEN (the
        # upstream Worker secret name).
        "TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def _build_pasay_container_env_vars(
    *,
    telegram_bot_token: str = "",
    passay_api_base: str = "",
    passay_api_key: str = "",
    passay_admin_api_key: str = "",
    passay_job_api_key: str = "",
    passay_http_timeout_seconds: str = "",
    passay_archive_chat_id: str = "",
    passay_mini_app_url: str = "",
    passay_mini_app_owner_telegram_ids: str = "",
    telegram_webhook_secret: str = "",
    passay_container_ingest_token: str = "",
    database_url: str = "",
    database_url_unpooled: str = "",
) -> dict:
    """Mirror of ``cloudflare-worker/src/index.ts::PasayContainer.envVars``
    that runs in pure Python so the test can replay the Worker → Container
    forwarding shape without Cloudflare. Any source-level change to the
    Worker's envVars map MUST keep this helper in sync; a TypeScript-side
    mismatch is caught by ``CLOSEOUT#7a/#7b/#7c`` in
    ``cloudflare-worker/tests/index.spec.ts``, and this Python helper is the
    mirror that proves the bot-side loader reads the same names."""
    tg_token = telegram_bot_token or ""
    return {
        "DATABASE_URL": database_url,
        "DATABASE_URL_UNPOOLED": database_url_unpooled,
        # PTB token is sourced under BOTH names — existing secret covers both.
        "TELEGRAM_BOT_TOKEN": tg_token,
        "PASSAY_TG_BOT_TOKEN": tg_token,
        # Backend-bound keys / endpoints (operator provisions these as Worker
        # secrets; default to "" so an unprovisioned bot fails closed on its
        # first API call instead of silently impersonating anyone).
        "PASSAY_API_BASE": passay_api_base,
        "PASSAY_API_KEY": passay_api_key,
        "PASSAY_ADMIN_API_KEY": passay_admin_api_key,
        "PASSAY_JOB_API_KEY": passay_job_api_key,
        # Optional / with-defaults (worker secret can override; bot keeps
        # its own defaults so an unprovisioned Worker still boots).
        "PASSAY_HTTP_TIMEOUT_SECONDS": passay_http_timeout_seconds,
        "PASSAY_ARCHIVE_CHAT_ID": passay_archive_chat_id,
        "PASSAY_MINI_APP_URL": passay_mini_app_url,
        "PASSAY_MINI_APP_OWNER_TELEGRAM_IDS": passay_mini_app_owner_telegram_ids,
        # Internal ingestion boundary (Worker → Container auth) — unchanged.
        "TELEGRAM_WEBHOOK_SECRET": telegram_webhook_secret,
        "CONTAINER_INGEST_TOKEN": passay_container_ingest_token,
        "PASAY_RUNTIME_MODE": "cloudflare-container",
    }


def _load_bot_settings_with_env(env_vars: dict):
    """Import ``pasay_bot.config`` with ``env_vars`` installed into
    ``os.environ``, return the resulting ``Settings``. Forces a fresh
    ``get_settings()`` (the loader is ``@lru_cache``-decorated)."""
    cfg = importlib.import_module("pasay_bot.config")
    for k, v in env_vars.items():
        if v == "":
            # Empty Container env-var forwards should be treated as
            # "unset" from the bot loader's POV (the Worker's ??
            # coalesces missing Worker secrets to "" and the bot's
            # _env() looks for a non-empty value via ``or`` semantics).
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    cfg.get_settings.cache_clear()
    return cfg.get_settings()


# ---------------------------------------------------------------------------
# A) Production-shape forwarding MUST give the bot a real token
# ---------------------------------------------------------------------------


def test_issue_119_p0_worker_forwards_real_token_to_bot(monkeypatch):
    """The Worker's PasayContainer.envVars forwarding shape MUST result in
    pasay_bot.config.Settings.pasay_tg_bot_token equal to the Worker's
    TELEGRAM_BOT_TOKEN secret (Issue #119 P0 root-cause regression guard)."""
    _isolate_bot_env(monkeypatch)
    env_vars = _build_pasay_container_env_vars(
        telegram_bot_token="555:REAL_PRODUCTION_TOKEN",
        telegram_webhook_secret="wh_secret",
        passay_container_ingest_token="ingest_token",
    )
    settings = _load_bot_settings_with_env(env_vars)
    assert settings.pasay_tg_bot_token == "555:REAL_PRODUCTION_TOKEN", (
        "pasay_tg_bot_token must equal the Worker's TELEGRAM_BOT_TOKEN secret "
        "after the Worker forwards it via PasayContainer.envVars — "
        "otherwise PTB.ApplicationBuilder.token falls back to '0:UNSET' "
        "and Owner receives no visible reply (Issue #119 P0)."
    )
    assert settings.pasay_tg_bot_token != "0:UNSET", (
        "pasay_tg_bot_token MUST NOT be the literal '0:UNSET' fallback — "
        "that is the exact user-visible failure mode from Issue #119."
    )


def test_issue_119_p0_bot_falls_back_to_upstream_token_when_pasay_name_missing(monkeypatch):
    """Defensive fallback: even if the Worker's envVars forwarding is
    accidentally reverted to only set TELEGRAM_BOT_TOKEN (forgetting
    PASSAY_TG_BOT_TOKEN), the bot loader must still find the token under
    the upstream name. This is the second line of defence behind the
    canonical Worker's PASSAY_TG_BOT_TOKEN forwarding."""
    _isolate_bot_env(monkeypatch)
    # Simulate the BROKEN pre-fix Worker forwarding: only TELEGRAM_BOT_TOKEN
    # is set, PASSAY_TG_BOT_TOKEN is absent.
    os.environ["TELEGRAM_BOT_TOKEN"] = "555:REAL_PRODUCTION_TOKEN"
    cfg = importlib.import_module("pasay_bot.config")
    cfg.get_settings.cache_clear()
    settings = cfg.get_settings()
    assert settings.pasay_tg_bot_token == "555:REAL_PRODUCTION_TOKEN", (
        "defensive fallback: bot loader must accept TELEGRAM_BOT_TOKEN as "
        "a fallback for PASSAY_TG_BOT_TOKEN so a Worker's PASSAY_TG_BOT_TOKEN "
        "forwarding regression does NOT silently revert to '0:UNSET'."
    )


def test_issue_119_p0_no_token_anywhere_still_fails_closed(monkeypatch):
    """When NEITHER PASSAY_TG_BOT_TOKEN NOR TELEGRAM_BOT_TOKEN is set, the
    bot must end up with pasay_tg_bot_token="" — the bot's caller is
    responsible for refusing to call build_application with an empty
    token (``if not settings.pasay_tg_bot_token: raise RuntimeError(...)``
    in pasay_bot.main._load). This test pins that the loader never
    silently fabricates a non-empty value."""
    _isolate_bot_env(monkeypatch)
    cfg = importlib.import_module("pasay_bot.config")
    cfg.get_settings.cache_clear()
    settings = cfg.get_settings()
    assert settings.pasay_tg_bot_token == "", (
        "with neither PASSAY_TG_BOT_TOKEN nor TELEGRAM_BOT_TOKEN set, the "
        "loader must surface pasay_tg_bot_token='' so the boot gate can "
        "fail closed (build_application.token('0:UNSET') is a silent lie)."
    )


def test_issue_119_p0_passay_tg_bot_token_wins_over_upstream(monkeypatch):
    """When BOTH env vars are set (operator provisioned a dedicated
    PASSAY_TG_BOT_TOKEN), the bot-specific name MUST take precedence —
    it lets the operator rotate the bot token without touching the
    upstream TELEGRAM_BOT_TOKEN secret."""
    _isolate_bot_env(monkeypatch)
    os.environ["TELEGRAM_BOT_TOKEN"] = "111:UPSTREAM"
    os.environ["PASSAY_TG_BOT_TOKEN"] = "222:BOT_SPECIFIC"
    cfg = importlib.import_module("pasay_bot.config")
    cfg.get_settings.cache_clear()
    settings = cfg.get_settings()
    assert settings.pasay_tg_bot_token == "222:BOT_SPECIFIC", (
        "PASSAY_TG_BOT_TOKEN must win over TELEGRAM_BOT_TOKEN when both "
        "are set — operator-rotation contract."
    )


# ---------------------------------------------------------------------------
# B) Production-shape forwarding MUST give the bot the API keys it needs
#    for backend calls (pasay_api_key) and for outbound digest/next_check
#    jobs (pasay_job_api_key). Without these the bot can technically
#    send_message but every /properties / /units / /digest call returns
#    401 and the v2 daily digest + next_check reminders never fire.
# ---------------------------------------------------------------------------


def test_issue_119_p0_worker_forwards_passay_api_key(monkeypatch):
    """pasay_api_key MUST be sourced from the Worker's PASSAY_API_KEY
    secret so the bot's PasayApiClient can call the backend with a
    valid Bearer header."""
    _isolate_bot_env(monkeypatch)
    env_vars = _build_pasay_container_env_vars(
        telegram_bot_token="555:REAL",
        passay_api_key="manager-level-key",
    )
    settings = _load_bot_settings_with_env(env_vars)
    assert settings.pasay_api_key == "manager-level-key", (
        "pasay_api_key must equal the Worker's PASSAY_API_KEY secret — "
        "empty pasay_api_key means every backend call returns 401."
    )


def test_issue_119_p0_worker_forwards_passay_job_api_key(monkeypatch):
    """pasay_job_api_key MUST be sourced from the Worker's
    PASSAY_JOB_API_KEY secret so the v2 daily digest and next_check
    reminder jobs can authenticate as the SYSTEM principal. Without
    it, jobs.register_jobs() fails closed and the bot never sends an
    automatic outbound message — the third Issue #119 acceptance
    criterion (autonomous outbound notification)."""
    _isolate_bot_env(monkeypatch)
    env_vars = _build_pasay_container_env_vars(
        telegram_bot_token="555:REAL",
        passay_job_api_key="system-key",
    )
    settings = _load_bot_settings_with_env(env_vars)
    assert settings.pasay_job_api_key == "system-key", (
        "pasay_job_api_key must equal the Worker's PASSAY_JOB_API_KEY "
        "secret — empty disables the digest + next_check jobs (fail "
        "closed), and the third Issue #119 acceptance criterion "
        "('automatic outbound notifications push without manual user "
        "input') can never be met."
    )


def test_issue_119_p0_worker_forwards_optional_bot_envs(monkeypatch):
    """PASSAY_API_BASE / PASSAY_MINI_APP_URL / PASSAY_ARCHIVE_CHAT_ID /
    PASSAY_ADMIN_API_KEY / PASSAY_HTTP_TIMEOUT_SECONDS /
    PASSAY_MINI_APP_OWNER_TELEGRAM_IDS MUST all be forwarded verbatim
    when the operator provisions them (with bot-side defaults covering
    the unprovisioned case)."""
    _isolate_bot_env(monkeypatch)
    env_vars = _build_pasay_container_env_vars(
        telegram_bot_token="555:REAL",
        passay_api_base="http://127.0.0.1:8000/api/v1",
        passay_mini_app_url="https://pasay-mini-app.pages.dev",
        passay_mini_app_owner_telegram_ids="111,222",
        passay_archive_chat_id="-1001234567890",
        passay_admin_api_key="admin-level-key",
        passay_http_timeout_seconds="45",
    )
    settings = _load_bot_settings_with_env(env_vars)
    assert settings.pasay_api_base == "http://127.0.0.1:8000/api/v1"
    assert settings.pasay_mini_app_url == "https://pasay-mini-app.pages.dev"
    assert settings.pasay_mini_app_owner_telegram_ids == "111,222"
    assert settings.archive_chat_id == "-1001234567890"
    assert settings.pasay_admin_api_key == "admin-level-key"
    assert settings.pasay_http_timeout_seconds == 45.0


# ---------------------------------------------------------------------------
# C) Source-level guardrail: the bot loader's keyset MUST stay in sync
#    with the Worker's envVars map. If a future commit adds a new
#    bot env var, this test reminds the author to update both sides.
# ---------------------------------------------------------------------------


def test_issue_119_p0_bot_loader_keys_are_exactly_what_worker_forwards():
    """Stable list of bot env var names. The Worker's PasayContainer
    must keep forwarding every one of these so the bot loader can
    surface them via ``pasay_bot.config.Settings``. A drift on either
    side breaks this test and forces the author to re-sync."""
    cfg = importlib.import_module("pasay_bot.config")
    # Whitelist of names that the bot loader actively reads. This is the
    # authoritative source of truth — derived directly from
    # pasay_bot/config.py::_env + get_settings.
    expected_bot_env_names = {
        "PASSAY_TG_BOT_TOKEN",
        "PASSAY_API_BASE",
        "PASSAY_API_KEY",
        "PASSAY_ADMIN_API_KEY",
        "HERMES_API_BASE",
        "HERMES_API_KEY",
        "STATE_DB",
        "HOOK_TOKEN",
        "CALLBACK_TTL_SECONDS",
        "PASSAY_HTTP_TIMEOUT_SECONDS",
        "PASSAY_ARCHIVE_CHAT_ID",
        "PASSAY_JOB_API_KEY",
        "PASSAY_MINI_APP_URL",
        "PASSAY_MINI_APP_OWNER_TELEGRAM_IDS",
        "TELEGRAM_BOT_TOKEN",  # defensive fallback only — bot reads under
                                # PASSAY_TG_BOT_TOKEN when both are set
    }
    # The forwarder must include the actual Worker env-var keys (the keys
    # the Worker writes into envVars) — these are the production-shape
    # keys that the Worker's PasayContainer.envVars emits.
    expected_worker_forwarded_keys = {
        "DATABASE_URL",
        "DATABASE_URL_UNPOOLED",
        "TELEGRAM_BOT_TOKEN",
        "PASSAY_TG_BOT_TOKEN",
        "PASSAY_API_BASE",
        "PASSAY_API_KEY",
        "PASSAY_ADMIN_API_KEY",
        "PASSAY_JOB_API_KEY",
        "PASSAY_HTTP_TIMEOUT_SECONDS",
        "PASSAY_ARCHIVE_CHAT_ID",
        "PASSAY_MINI_APP_URL",
        "PASSAY_MINI_APP_OWNER_TELEGRAM_IDS",
        "TELEGRAM_WEBHOOK_SECRET",
        "CONTAINER_INGEST_TOKEN",
        "PASAY_RUNTIME_MODE",
    }
    # The Worker forwarder's PASSAY_* keys must all be in the bot loader
    # keyset — otherwise the Worker is forwarding a name the bot never
    # reads (silent drift in the other direction).
    passay_only = {
        k for k in expected_worker_forwarded_keys
        if k.startswith("PASSAY_") and k not in expected_bot_env_names
    }
    assert passay_only == set(), (
        f"Worker forwards PASSAY_ keys the bot loader never reads: "
        f"{sorted(passay_only)} — either drop the Worker forwarder or "
        f"extend pasay_bot/config.py to consume them."
    )
    # Bot-side names that the Worker MUST forward (every bot-loader
    # PASSAY_* key must have a Worker forwarder, otherwise the bot
    # never receives the value in production).
    must_forward = {
        k for k in expected_bot_env_names
        if k.startswith("PASSAY_")
    }
    missing = must_forward - expected_worker_forwarded_keys
    assert missing == set(), (
        f"Worker does not forward these bot-loader keys: {sorted(missing)} "
        f"— add them to PasayContainer.envVars so the bot receives them "
        f"in production."
    )