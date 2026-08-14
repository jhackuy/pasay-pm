"""Gate A: every Telegram entry surface binds effective_user.id, never chat id."""
import asyncio
import inspect

import pytest
from telegram import Update
from telegram.ext import CommandHandler

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    make_callback_update,
    make_text_update,
)
from pasay_bot.handlers import commands
from pasay_bot.keyboards import encode


GROUP_CHAT_ID = -100777888999


def test_registered_commands_bind_identity_before_page_or_api_work(make_app):
    env = make_app()
    callbacks = [
        handler.callback
        for handlers in env.app.handlers.values()
        for handler in handlers
        if isinstance(handler, CommandHandler)
    ]
    assert {callback.__name__ for callback in callbacks} == {
        "cmd_start",
        "cmd_help",
        "cmd_cancel",
    }
    for callback in callbacks:
        source = inspect.getsource(callback)
        assert "_bind_identity" in source or "cmd_start" in source


def test_command_callback_conversation_and_nl_bridge_separate_group_users(make_app):
    env = make_app()
    env.backend.add_ops_task(task_id=91)
    env.store.save_conversation(
        GROUP_CHAT_ID,
        OWNER_ID,
        "ops_snooze_custom",
        {"task_id": 91},
    )
    updates = [
        ("command", make_text_update(
            OWNER_ID, GROUP_CHAT_ID, "/start", update_id=101, bot=env.bot
        )),
        ("callback", make_callback_update(
            SECRETARY_ID,
            GROUP_CHAT_ID,
            encode("nav", "finance"),
            update_id=102,
            bot=env.bot,
        )),
        ("conversation", make_text_update(
            OWNER_ID, GROUP_CHAT_ID, "1h", update_id=103, bot=env.bot
        )),
        ("nl_bridge", make_text_update(
            SECRETARY_ID, GROUP_CHAT_ID, "properties", update_id=104, bot=env.bot
        )),
    ]
    expected = {
        "command": str(OWNER_ID),
        "callback": str(SECRETARY_ID),
        "conversation": str(OWNER_ID),
        "nl_bridge": str(SECRETARY_ID),
    }

    async def scenario():
        segments = {}
        async with env.app:
            for label, update in updates:
                before = len(env.backend.telegram_user_calls)
                await env.app.process_update(update)
                segments[label] = env.backend.telegram_user_calls[before:]
        return segments

    segments = asyncio.run(scenario())
    for label, identity_headers in segments.items():
        assert identity_headers, f"{label} did not make its expected API call"
        assert set(identity_headers) == {expected[label]}
        assert str(GROUP_CHAT_ID) not in identity_headers
    assert env.api._telegram_user_id.get() is None
    assert env.admin_api._telegram_user_id.get() is None


def test_bind_identity_rejects_missing_effective_user_before_api(make_app):
    env = make_app()
    env.api.bind_telegram_user(OWNER_ID)
    env.admin_api.bind_telegram_user(OWNER_ID)
    update = Update(update_id=999)
    with pytest.raises(ValueError):
        commands._bind_identity(update, env.app)
    assert env.backend.calls == []
    assert env.api._telegram_user_id.get() is None
    assert env.admin_api._telegram_user_id.get() is None
