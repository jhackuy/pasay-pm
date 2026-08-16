"""Entry point: ApplicationBuilder -> getMe self-check -> run_polling().

--dry-run runs only the getMe self-check (used by bin/start-native-bot.sh and
for local verification) and exits without starting polling.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

import telegram
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from pasay_bot.api_client import PasayApiClient
from pasay_bot.config import Settings, get_settings
from pasay_bot.handlers import callback as callback_handlers
from pasay_bot.handlers import commands, conversation
from pasay_bot.state.idempotency import IdempotencyGuard
from pasay_bot.state.latency import LatencyTracker
from pasay_bot.state.store import StateStore

from telegram import Update  # noqa: F401  (Update.ALL_TYPES for explicit allowed_updates)
from telegram.request import HTTPXRequest

logger = logging.getLogger(__name__)


class _TraceUpdatesRequest(HTTPXRequest):
    """Minimal BaseRequest tracing wrapper for the official getUpdates request.

    Logs each getUpdates POST (endpoint / offset / timeout / allowed_updates, with
    auth token redacted from the URL) and the returned update ids, so the
    production consumer's own poller is observed without a competing probe or
    monkey-patching ExtBot. Used only while the P0 live-diagnostic is active.
    """

    async def post(self, url, request_data=None, read_timeout=HTTPXRequest.DEFAULT_NONE,
                   write_timeout=HTTPXRequest.DEFAULT_NONE, connect_timeout=HTTPXRequest.DEFAULT_NONE,
                   pool_timeout=HTTPXRequest.DEFAULT_NONE):
        try:
            params = dict(request_data.parameters) if request_data is not None else {}
            # Redact the auth token and any secret-like value; keep the rest for debugging.
            params.pop("token", None)
            print("[GU] getUpdates POST url=%s params=%r" % (
                url.replace("://api.telegram.org/bot", "://api.telegram.org/bot<REDACTED>"),
                params,
            ), flush=True)
        except Exception:
            pass
        try:
            json_data = await super().post(
                url, request_data=request_data,
                read_timeout=read_timeout, write_timeout=write_timeout,
                connect_timeout=connect_timeout, pool_timeout=pool_timeout,
            )
        except Exception as _e:
            print("[GU] getUpdates EXC %r" % (_e,), flush=True)
            raise
        if isinstance(json_data, list):
            print("[GU] getUpdates RETURN count=%d ids=%r" % (
                len(json_data), [getattr(u, "update_id", None) for u in json_data]
                if json_data and isinstance(json_data[0], (int, dict)) else [getattr(u, "update_id", None) for u in json_data]),
                flush=True)
        return json_data


def build_application(
    settings: Settings,
    api_client: PasayApiClient,
    store: StateStore,
    bot=None,
    admin_api_client: PasayApiClient | None = None,
) -> Application:
    builder = Application.builder()
    if bot is not None:
        builder = builder.bot(bot)  # tests / custom bot
    else:
        timeout = settings.pasay_http_timeout_seconds
        builder = (
            builder
            .token(settings.pasay_tg_bot_token or "0:UNSET")
            .connect_timeout(timeout)
            .read_timeout(timeout)
            .write_timeout(timeout)
            .pool_timeout(timeout)
            # P0 live-diagnostic: wrap ONLY the official getUpdates request in a
            # tracing request object (no monkey-patching of ExtBot, no competing
            # probe) so we can observe the producer's poll within this consumer.
            .get_updates_request(_TraceUpdatesRequest())
        )
    app = builder.build()
    app.bot_data["api_client"] = api_client
    app.bot_data["admin_api_client"] = admin_api_client
    app.bot_data["store"] = store
    app.bot_data["settings"] = settings
    app.bot_data["idempotency"] = IdempotencyGuard(store)
    # Code-side handler latency instrumentation (never an LLM judgment).
    app.bot_data["latency"] = LatencyTracker()

    # PASAY-V2-FOUNDATION-001: only rescue commands remain on the slash menu.
    # Business commands (/rent, /expense, /properties, /tasks, /help, ...)
    # are handled by the fixed keyboard / normal chat instead — users never
    # need to know /start.
    app.add_handler(CommandHandler("start", commands.cmd_start))
    app.add_handler(CommandHandler("help", commands.cmd_help))
    app.add_handler(CommandHandler("cancel", commands.cmd_cancel))
    # SLICE3-UX-PERSISTENT-MENU-002: group new-member onboarding (neutral
    # welcome only; ReplyKeyboardMarkup is per-chat, so no role menu broadcast).
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS, commands.handle_new_chat_members
        )
    )
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.ALL, commands.handle_media_message
        )
    )
    app.add_handler(CallbackQueryHandler(callback_handlers.handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, conversation.handle_message))

    # PASAY-V2-FOUNDATION-001: daily digest + next_check reminders live in
    # pasay_bot/jobs.py (Subagent C). The seam below keeps the wiring
    # decoupled so the app stays runnable while the module lands in parallel.
    try:
        from pasay_bot import jobs

        jobs.register_jobs(app, api_client, store, settings)
    except Exception as exc:  # noqa: BLE001 - wiring must never block startup
        logger.warning("jobs.register_jobs failed: %s", exc)

    async def _set_rescue_command_menu(app_):
        """Advertise ONLY the rescue commands in the BotFather menu."""
        try:
            await app_.bot.set_my_commands(
                [
                    ("start", "Start (recovery)"),
                    ("help", "Help"),
                    ("cancel", "Cancel"),
                ]
            )
        except Exception as exc:  # noqa: BLE001 - cosmetic, never fatal
            logger.warning("set_my_commands failed: %s", exc)

    app.post_init = _set_rescue_command_menu
    return app


async def self_check(app: Application) -> str:
    me = await app.bot.get_me()
    return f"getMe OK: @{me.username} (id={me.id})"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pasay_bot.main")
    parser.add_argument("--dry-run", action="store_true",
                        help="getMe self-check only, then exit (no polling)")
    parser.add_argument("--state-db", default=None, help="override STATE_DB path")
    parser.add_argument("--token", default=None, help="override bot token (testing)")
    return parser.parse_args(argv)


def _load(args: argparse.Namespace) -> tuple[Settings, StateStore, PasayApiClient, PasayApiClient | None]:
    settings = get_settings()
    if args.token:
        settings = settings.model_copy(update={"pasay_tg_bot_token": args.token})
    if args.state_db:
        settings = settings.model_copy(update={"state_db": args.state_db})
    if not settings.pasay_tg_bot_token:
        raise RuntimeError("PASSAY_TG_BOT_TOKEN is not set (set it in .env or pass --token).")
    store = StateStore(settings.state_db)
    store.migrate()
    api = PasayApiClient(settings.pasay_api_base, settings.pasay_api_key)
    admin_api = (
        PasayApiClient(settings.pasay_api_base, settings.pasay_admin_api_key)
        if settings.pasay_admin_api_key
        else None
    )
    return settings, store, api, admin_api


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        settings, store, api, admin_api = _load(args)
    except RuntimeError as exc:
        print(f"[pasay-bot] {exc}", file=sys.stderr)
        return 2

    # Self-check (getMe) in its own short-lived event loop so it never
    # conflicts with PTB's polling loop below.
    try:
        asyncio.run(_self_check(settings))
        if args.dry_run:
            print("[pasay-bot] dry-run OK; not starting polling.")
            return 0
    except Exception as exc:  # noqa: BLE001 - fail closed with a clear message
        print(f"[pasay-bot] self-check failed: {exc}", file=sys.stderr)
        return 1

    try:
        # run_polling() is BLOCKING and manages its OWN event loop; call it at
        # top level (not inside asyncio.run) so its loop isn't shared/closed by
        # an outer runner.
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            app = build_application(settings, api, store, admin_api_client=admin_api)
            print("[pasay-bot] starting polling ...")
            # ---- P0 live-diagnostic: A1/A2/A3/A4 production-chain tracing ----
            try:
                # (A1) get_updates RETURN is observed indirectly: ExtBot forbids
                #      patching get_updates, so we rely on [A2] queue.put AFTER as the
                #      proof that get_updates returned the update; an alive polling task
                #      (task-probe below) + NO put on the tap => get_updates returned 0.

                # (A2) update_queue.put: BEFORE / AFTER / size
                _orig_put = app.update_queue.put
                async def _traced_put(item):
                    print("[A2] queue.put BEFORE qsize=%d item_type=%s" % (
                        app.update_queue.qsize(), type(item).__name__), flush=True)
                    try:
                        await _orig_put(item)
                        print("[A2] queue.put AFTER qsize=%d" % app.update_queue.qsize(), flush=True)
                    except Exception as _e:
                        print("[A2] queue.put EXC %r" % (_e,), flush=True)
                        raise
                app.update_queue.put = _traced_put

                # (A3) update_queue.get (consumer): GET
                _orig_get_q = app.update_queue.get
                async def _traced_get_q():
                    item = await _orig_get_q()
                    eff_msg = getattr(item, "effective_message", None)
                    print("[A3] queue.get update_id=%s qsize_after=%d text=%r" % (
                        getattr(item, "update_id", None),
                        app.update_queue.qsize(),
                        getattr(eff_msg, "text", None) if eff_msg else None,
                    ), flush=True)
                    return item
                app.update_queue.get = _traced_get_q

                # (A4) process_update ENTER (bound method replace)
                _ORIG_PROCESS_UPDATE = app.process_update
                async def _traced_process_update(update):
                    eff_msg = getattr(update, "effective_message", None)
                    eff_chat = getattr(update, "effective_chat", None)
                    eff_user = getattr(update, "effective_user", None)
                    print("[A4] process_update ENTER update_id=%s type=%s message_id=%s chat_id=%s user_id=%s text=%r" % (
                        getattr(update, "update_id", None),
                        getattr(update, "update_type", None),
                        getattr(eff_msg, "message_id", None) if eff_msg else None,
                        getattr(eff_chat, "id", None) if eff_chat else None,
                        getattr(eff_user, "id", None) if eff_user else None,
                        getattr(eff_msg, "text", None) if eff_msg else None,
                    ), flush=True)
                    try:
                        return await _ORIG_PROCESS_UPDATE(update)
                    except Exception as _e:
                        print("[A4] process_update EXC %r" % (_e,), flush=True)
                        raise
                app.process_update = _traced_process_update

                # Handler inventory (registration + filters).
                print("[PTB] handler inventory:", flush=True)
                for group in sorted(app.handlers.keys()):
                    for h in app.handlers[group]:
                        _filt = getattr(h, "filters", None)
                        try:
                            _filt_repr = repr(_filt)
                        except Exception:
                            _filt_repr = str(type(_filt).__name__)
                        print("[PTB]   group=%s class=%s filters=%s" % (
                            group, type(h).__name__, _filt_repr), flush=True)
                print("[PTB] handler inventory done", flush=True)
            except Exception as _diag:
                print("[PTB] diag setup failed: %r" % (_diag,), flush=True)
            # Periodic task-health probe (asyncio tasks alive/done/cancelled) — only
            # makes sense once run_polling's loop is running, so schedule it via
            # app.job_queue (PTB schedules after initialization inside run_polling).
            try:
                import asyncio as _asyncio_mod

                def _dump_tasks():
                    _tasks = [t for t in _asyncio_mod.all_tasks() if not t.done()]
                    print("[TASK] alive asyncio tasks count=%d:" % len(_tasks), flush=True)
                    for t in _tasks:
                        try:
                            _nm = t.get_name()
                        except Exception:
                            _nm = '?'
                        _ex = 'n/a'
                        if t.done() and not t.cancelled():
                            try:
                                _ex = repr(t.exception())
                            except Exception:
                                _ex = 'unknown'
                        print("[TASK]   done=%s cancelled=%s ex=%s name=%r" % (
                            t.done(), t.cancelled(), _ex, _nm), flush=True)

                _jq = getattr(app, "job_queue", None)
                if _jq is not None:
                    _jq.run_repeating(lambda _c: _dump_tasks(), interval=25, first=5,
                                      name="P0_task_health")
                    print("[TASK] probe registered via job_queue", flush=True)
                else:
                    print("[TASK] probe scheduling skipped (no job_queue)", flush=True)
            except Exception as _diag3:
                print("[TASK] probe registration failed: %r" % (_diag3,), flush=True)
            # ---- end P0 live-diagnostic ----
            try:
                # Explicit allowed_updates: PTB omits the field when allowed_updates is
                # None (see request/_requestdata.py filtering), and Telegram then keeps
                # the previously-persisted (possibly restrictive) update types, which can
                # silently drop `message` updates. Pass the full set so business types
                # (message, callback_query, ...) are always subscribed. The official PTB
                # example uses Update.ALL_TYPES here.
                app.run_polling(allowed_updates=Update.ALL_TYPES)
                return 0
            except telegram.error.TimedOut as exc:
                print(
                    f"[pasay-bot] polling network timeout "
                    f"(attempt {attempt}/{max_retries}): {exc}",
                    file=sys.stderr,
                )
                if attempt >= max_retries:
                    print("[pasay-bot] polling failed after retries", file=sys.stderr)
                    return 1
                time.sleep(5)
    except Exception as exc:  # noqa: BLE001 - fail closed
        print(f"[pasay-bot] self-check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            import asyncio as _asyncio
            _asyncio.run(api.aclose())
        except Exception:
            pass
        if admin_api is not None:
            try:
                import asyncio as _asyncio
                _asyncio.run(admin_api.aclose())
            except Exception:
                pass
        store.close()


async def _self_check(settings: Settings) -> None:
    # Standalone lightweight bot so we don't disturb the Application lifecycle
    # that run_polling() manages (using `async with app.bot` would pre-close the
    # bot's event-loop context -> "Cannot close a running event loop").
    from telegram.request import HTTPXRequest
    timeout = settings.pasay_http_timeout_seconds
    request = HTTPXRequest(
        connect_timeout=timeout,
        read_timeout=timeout,
        write_timeout=timeout,
        pool_timeout=timeout,
    )
    async with telegram.Bot(
        settings.pasay_tg_bot_token, request=request
    ) as selfcheck_bot:
        me = await selfcheck_bot.get_me()
        print(f"[pasay-bot] getMe OK: @{me.username} (id={me.id})")


if __name__ == "__main__":
    sys.exit(main())
