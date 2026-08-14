"""main.py plumbing: build_application + getMe self-check (no network)."""
import asyncio

from conftest import FakeBackend, FakeBot
from pasay_bot.api_client import PasayApiClient
from pasay_bot.config import Settings
from pasay_bot.main import build_application, self_check
from pasay_bot.state.store import StateStore


def test_self_check_uses_get_me(make_app):
    env = make_app()
    result = asyncio.run(self_check(env.app))
    assert result == "getMe OK: @pasay_test_bot (id=999)"


def test_build_application_registers_handlers(make_app):
    env = make_app()
    commands = []
    for handlers in env.app.handlers.values():
        for h in handlers:
            name = type(h).__name__
            if name == "CommandHandler":
                commands.extend(h.commands)
    # PASAY-V2-FOUNDATION-001: only rescue commands remain registered.
    assert {"start", "help", "cancel"} <= set(commands)
