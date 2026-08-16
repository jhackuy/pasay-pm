"""P0-TELEGRAM-DISPATCH-CONSUMER-001 regression tests.

Root cause (proven live): the A3/A4 live-diagnostic ``print()`` of user text
raised ``UnicodeEncodeError`` on the Windows stream codec (GBK/cp936) when the
message contained a character outside the codec (e.g. emoji 🏠). The exception
propagated out of the traced ``queue.get`` and killed the Application
update-fetcher consumer task, so ``queue.get`` never ran again: the update
queue grew forever and users received no replies.

Fix: :func:`pasay_bot.main.harden_stdio` reconfigures stdout/stderr to UTF-8 +
backslashreplace (never raises), and every diagnostic print goes through
:func:`pasay_bot.main._safe_trace` (never raises). The task-health probe
callback is a coroutine function so PTB's ``await self.callback(context)`` no
longer raises "TypeError: object NoneType can't be used in 'await' expression".
"""
import asyncio
import io
import sys

import pytest

from conftest import OWNER_ID, make_text_update
from pasay_bot.main import _safe_trace, harden_stdio, install_live_diagnostics

EMOJI_TEXT = "🏠 16B 房租"


def _gbk_stream() -> io.TextIOWrapper:
    """Simulate the Windows Chinese-locale stream (GBK, strict)."""
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict",
                            line_buffering=True)


def test_safe_trace_never_raises_on_non_gbk_text(monkeypatch):
    """The exact pre-fix crash: a raw print of emoji text raises on GBK. The
    diagnostic helper must swallow it so the consumer task can never be killed
    by a trace print."""
    monkeypatch.setattr(sys, "stdout", _gbk_stream())
    with pytest.raises(UnicodeEncodeError):
        print(EMOJI_TEXT)  # pre-fix behavior that killed the consumer
    _safe_trace(EMOJI_TEXT)  # must not raise


def test_harden_stdio_prevents_encode_error(monkeypatch):
    """main() reconfigures the streams; after that printing emoji text cannot
    raise, which protects the handlers' own [TRACE] prints too."""
    monkeypatch.setattr(sys, "stdout", _gbk_stream())
    harden_stdio()
    print(EMOJI_TEXT)  # must not raise


def test_consumer_survives_emoji_update(make_app, monkeypatch):
    """End-to-end: with the production diagnostic wiring and a stream that
    would previously crash, the update-fetcher consumer must drain the queue,
    process the update and stay alive."""
    env = make_app()
    app = env.app
    install_live_diagnostics(app)

    # Simulate the Windows locale stream, then apply the production fix exactly
    # like main() does before anything is printed.
    monkeypatch.setattr(sys, "stdout", _gbk_stream())
    monkeypatch.setattr(sys, "stderr", _gbk_stream())
    harden_stdio()

    async def run():
        await app.initialize()
        if app.post_init:
            await app.post_init(app)
        await app.start()  # creates the update-fetcher consumer task
        fetcher = app._Application__update_fetcher_task
        try:
            update = make_text_update(
                OWNER_ID, OWNER_ID, EMOJI_TEXT,
                message_id=101, update_id=1001, bot=env.bot,
            )
            await app.update_queue.put(update)
            await asyncio.sleep(2.0)
            assert app.update_queue.qsize() == 0, (
                "queue not drained — consumer did not process the update"
            )
            assert not fetcher.done(), (
                "consumer task died while processing an emoji update"
            )
            assert not fetcher.cancelled()
        finally:
            await app.stop()
            await app.shutdown()

    asyncio.run(run())
    assert env.bot.calls, "no bot call was made for the update"


def test_task_health_probe_does_not_raise(make_app, capsys):
    """The P0_task_health job must tick without the old
    'TypeError: object NoneType can't be used in await' crash."""
    env = make_app()
    app = env.app
    install_live_diagnostics(app)

    async def run():
        await app.initialize()
        if app.post_init:
            await app.post_init(app)
        await app.start()
        try:
            await asyncio.sleep(6.5)  # probe first=5 -> ticked at least once
        finally:
            await app.stop()
            await app.shutdown()

    asyncio.run(run())
    captured = capsys.readouterr()
    assert "[TASK] probe registered via job_queue" in captured.out
    assert "[TASK] alive asyncio tasks" in captured.out, "probe never ticked"
    assert "NoneType can't be used in 'await'" not in (captured.out + captured.err)
