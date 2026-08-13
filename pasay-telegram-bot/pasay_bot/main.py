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
from pasay_bot.state.store import StateStore

logger = logging.getLogger(__name__)


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
        )
    app = builder.build()
    app.bot_data["api_client"] = api_client
    app.bot_data["admin_api_client"] = admin_api_client
    app.bot_data["store"] = store
    app.bot_data["settings"] = settings
    app.bot_data["idempotency"] = IdempotencyGuard(store)

    app.add_handler(CommandHandler("start", commands.cmd_start))
    app.add_handler(CommandHandler("menu", commands.cmd_menu))
    app.add_handler(CommandHandler("help", commands.cmd_help))
    app.add_handler(CommandHandler("properties", commands.cmd_properties))
    app.add_handler(CommandHandler("finance", commands.cmd_finance))
    app.add_handler(CommandHandler("overdue", commands.cmd_overdue))
    app.add_handler(CommandHandler("rent", commands.cmd_rent))
    app.add_handler(CommandHandler("pending", commands.cmd_pending))
    app.add_handler(CommandHandler("cancel", commands.cmd_cancel))
    app.add_handler(CommandHandler("ops", commands.cmd_ops))
    app.add_handler(CommandHandler("todo", commands.cmd_todo))
    app.add_handler(CommandHandler("copilot", commands.cmd_copilot))
    app.add_handler(CallbackQueryHandler(callback_handlers.handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, conversation.handle_message))
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
            try:
                app.run_polling()
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
