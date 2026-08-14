"""P0-TELEGRAM-AUTH-ROUTING-002 targeted tests.

Deterministic fixed bottom-menu buttons (home / pending / rent / expense)
must:
* route Telegram Bot -> Backend deterministically, never through
  Hermes/LLM/Property Plugin;
* send ``X-Telegram-User-Id`` = real ``update.effective_user.id`` (never the
  chat id, never a fixed Owner fallback);
* stay 401-free once the identity is bound (backend acceptance is covered by
  the live deploy smoke against the real runtime).
"""
from __future__ import annotations

import pathlib

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    make_text_update,
    run_updates,
)
from pasay_bot.keyboards import FIXED_MENU_ROUTES

GROUP_CHAT_ID = -100777888999
_BOT_PKG = pathlib.Path(__file__).resolve().parents[1] / "pasay_bot"


def test_fixed_menu_buttons_send_real_sender_id_not_chat_or_fallback(make_app):
    """Owner and Secretary clicks on all four fixed menus send their own real
    Telegram user id (never the group chat id, never a fixed Owner fallback)
    and the deterministic page renders (backend 200 in the fake).

    PASAY-V2-FOUNDATION-001: every fixed button is a deterministic Quick
    View that makes exactly ONE backend read with the sender's identity.
    """
    env = make_app()
    for label in FIXED_MENU_ROUTES:
        for user_id in (OWNER_ID, SECRETARY_ID):
            run_updates(
                env, [make_text_update(user_id, GROUP_CHAT_ID, label, bot=env.bot)]
            )
            headers = env.backend.telegram_user_calls
            assert headers, f"{label} (user {user_id}) made no backend call"
            assert set(headers) == {str(user_id)}, (
                f"{label} (user {user_id}) sent wrong identity headers: {headers}"
            )
            assert str(GROUP_CHAT_ID) not in headers
            assert str(OWNER_ID) not in headers or user_id == OWNER_ID
            env.backend.telegram_user_calls.clear()


def test_no_fixed_owner_telegram_id_fallback_in_bot_chain():
    """The bot chain never falls back to a hard-coded Owner Telegram id or a
    PROPERTY_TELEGRAM_USER_ID-style credential override."""
    targets = (
        "api_client.py",
        "config.py",
        "main.py",
        "handlers/buttons.py",
        "handlers/conversation.py",
    )
    for rel in targets:
        src = (_BOT_PKG / rel).read_text(encoding="utf-8")
        assert "PROPERTY_TELEGRAM_USER_ID" not in src, rel
        assert "5177241442" not in src, rel


def test_fixed_menu_handler_isolated_from_llm_hermes_plugin():
    """The fixed-menu handler source must not reference the NL/LLM, Hermes or
    Property Plugin machinery at all."""
    src = (_BOT_PKG / "handlers" / "buttons.py").read_text(encoding="utf-8").lower()
    for token in ("nl_bridge", "handle_nl", "hermes", "plugin", "copilot"):
        assert token not in src, f"buttons.py must not reference {token}"
