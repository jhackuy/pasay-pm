"""main.py plumbing: build_application + getMe self-check (no network)."""
import asyncio

from conftest import FakeBackend, FakeBot
from pasay_bot.api_client import PasayApiClient
from pasay_bot.config import Settings
from pasay_bot.main import build_application, self_check
from pasay_bot.roles import Role
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
    commands; only safe rescue commands (start/help/cancel) are set. The
    frozen 6-button fixed Reply Keyboard (Issue #119 menu routing) MUST be
    intact for both Owner (zh) and Secretary (en)."""
    env = make_app()
    # The bot's set_my_commands payload is whatever the app posts; assert the
    # registered CommandHandler set has no dev command and the fixed reply
    # keyboard (six buttons per role) is untouched.
    commands = set()
    for handlers in env.app.handlers.values():
        for h in handlers:
            if type(h).__name__ == "CommandHandler":
                commands.update(h.commands)
    assert "stop" not in commands and "stress" not in commands
    assert "new" not in commands and "status" not in commands
    from pasay_bot.keyboards import reply_keyboard
    # Issue #119 A: the keyboard MUST carry the six frozen IA buttons (Home /
    # Properties / Tasks / Rent / Expense / Archive). The owner-facing 6-button
    # keyboard is what failed to reply in production; this guardrail keeps the
    # menu and its exact labels in lock-step with the routing table.
    owner_labels = [b.text for row in reply_keyboard(Role.OWNER).keyboard for b in row]
    secretary_labels = [
        b.text for row in reply_keyboard(Role.SECRETARY).keyboard for b in row
    ]
    assert owner_labels == ["🏠 首页", "🏘 房源", "✅ 待办",
                            "💰 租金", "💸 支出", "📁 档案"]
    assert secretary_labels == ["🏠 Home", "🏘 Properties", "✅ Tasks",
                                "💰 Rent", "💸 Expense", "📁 Archive"]
