"""Idempotent edit_message_text helper.

Telegram raises BadRequest("Message is not modified") when the new content is
identical to the current message. Treat that as a no-op instead of a failure,
so re-clicking a button that re-renders the same card does not error out or
spam err.log; any other BadRequest still propagates.
"""
from __future__ import annotations

from telegram.error import BadRequest

_NOOP_MESSAGE = "Message is not modified"


async def edit_message_text_idempotent(bot, *, chat_id, message_id, text,
                                       parse_mode=None, reply_markup=None):
    """Wrap bot.edit_message_text, swallowing only the no-op BadRequest."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        if _NOOP_MESSAGE not in str(exc):
            raise


async def edit_message_text_or_send(bot, *, chat_id, message_id, text,
                                    parse_mode=None, reply_markup=None):
    """Edit-first with a send fallback (V1.3): if the edit fails for any reason
    (message deleted / bot restarted / unexpected BadRequest), send the same
    content as a new message instead of dropping the user's action result."""
    try:
        await edit_message_text_idempotent(
            bot,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except Exception:  # noqa: BLE001 - fallback must never lose the result
        await bot.send_message(
            chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup
        )
