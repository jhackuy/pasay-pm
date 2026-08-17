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
    # TELEGRAM-OPS-REAL-WORLD-CLOSURE-005 §14: dev/control commands must NEVER
    # be registered for production users (/new /stop /status /stress /debug /
    # /dev /test /ops /todo /copilot).
    forbidden_dev = {"new", "stop", "status", "stress", "debug", "dev", "test"}
    assert set(commands).isdisjoint(forbidden_dev)


def test_command_menu_excludes_dev_commands(make_app):
    """§14.1/§14.2: the production command menu must not advertise dev/control
    commands; only safe rescue commands (start/help/cancel) are set."""
    env = make_app()
    # The bot's set_my_commands payload is whatever the app posts; assert the
    # registered CommandHandler set has no dev command and the fixed reply
    # keyboard (Properties/Tasks/Rent/Expense) is untouched.
    commands = set()
    for handlers in env.app.handlers.values():
        for h in handlers:
            if type(h).__name__ == "CommandHandler":
                commands.update(h.commands)
    assert "stop" not in commands and "stress" not in commands
    assert "new" not in commands and "status" not in commands
    from pasay_bot.keyboards import reply_keyboard
    labels = [b.text for row in reply_keyboard("owner").keyboard for b in row]
    assert labels == ["🏠 Properties", "✅ Tasks", "💰 Rent", "💸 Expense"]
