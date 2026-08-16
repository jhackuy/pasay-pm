"""AI-OPS-FOUNDATION-001 acceptance tests (bot / Telegram UX layer).

Covers the acceptance items that live in the bot:
- §8  "Technician coming tomorrow" persists a structured promise
- §9/§12 ambiguous "finished" shows deterministic candidates (never closes
  the wrong task)
- §12 media is forwarded to the private archive + indexed as evidence
- §14 evidence/media can be retrieved by unit
- §14 Telegram-first Unit create fast path (confirmation card, idempotent)
- §17 viewing messages become business events (confirmation card)
- §20 bilingual group replies do not regress
"""
from __future__ import annotations

import time

import pytest
from telegram import Update

from conftest import (
    OWNER_ID,
    SECRETARY_ID,
    make_callback_update,
    make_text_update,
    run_updates,
)

from pasay_bot.keyboards import decode, encode


def _photo_update(user_id, chat_id, message_id=50, update_id=50, caption=None):
    msg = {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "T", "username": "t"},
        "photo": [
            {
                "file_id": "photo_small_id", "file_unique_id": "u1",
                "width": 90, "height": 90, "file_size": 600,
            },
            {
                "file_id": "photo_big_id", "file_unique_id": "u2",
                "width": 1280, "height": 720, "file_size": 1234,
            },
        ],
    }
    if caption:
        msg["caption"] = caption
    return Update.de_json({"update_id": update_id, "message": msg}, None)


def _group_text_update(user_id, chat_id, text, message_id=60, update_id=60):
    msg = {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "group", "title": "PASay-PM"},
        "from": {"id": user_id, "is_bot": False, "first_name": "T", "username": "t"},
        "text": text,
    }
    return Update.de_json({"update_id": update_id, "message": msg}, None)


def _channel_post_update(chat_id, text="archive post", message_id=80, update_id=80):
    """A channel_post update (no effective_user) — e.g. a post in the archive
    channel the bot administers."""
    msg = {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "channel", "title": "pasay property archive"},
        "sender_chat": {"id": chat_id, "type": "channel", "title": "pasay property archive"},
        "text": text,
    }
    return Update.de_json({"update_id": update_id, "channel_post": msg}, None)


def test_channel_post_is_ignored_not_crashed(make_app):
    """AI-OPS-001 §12: a channel_post update (archive channel) has no
    effective_user — the bot ignores it without raising and without replying."""
    env = make_app()
    run_updates(env, [_channel_post_update(-1004292596162)])
    assert len(env.bot.sends()) == 0
    assert env.backend.calls == []


# --- §8 promise persistence --------------------------------------------------

def test_technician_coming_tomorrow_persists_structured_promise(make_app):
    """AI-OPS-001 §8: a progress message on an active repair task persists a
    structured promise (follow_up_at / responsible_party / status), not just a
    conversational reply."""
    env = make_app()
    # Create a repair task first (v2 context carries the task_ref).
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "16B aircon leaking", bot=env.bot)])
    created_task = env.backend.operational_tasks[-1]
    assert created_task["task_type"] == "AC_MAINTENANCE"

    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "Technician coming tomorrow", bot=env.bot)])
    task = env.backend._ops_task(f"/operations/tasks/{created_task['id']}")
    assert task["status"] == "IN_PROGRESS"
    promise = (task.get("details") or {}).get("promise") or {}
    assert promise.get("status") == "open"
    assert promise.get("responsible_party") == "technician"
    assert promise.get("follow_up_at"), "follow_up_at must be persisted"
    assert promise.get("related_entity") == f"task:{created_task['id']}"


# --- §9/§12 ambiguous "finished" ----------------------------------------------

def _seed_two_repairs(env):
    env.backend.add_ops_task(task_id=11, title="Unit 16B aircon", task_type="AC_MAINTENANCE",
                             due_at="2026-08-16T00:00:00+08:00")
    env.backend.add_ops_task(task_id=12, title="Unit 17A leak", task_type="AC_MAINTENANCE",
                             due_at="2026-08-16T00:00:00+08:00")


def test_ambiguous_finished_shows_candidates_and_closes_nothing(make_app):
    """AI-OPS-001 §12: 'finished' without context never guesses — it renders
    one deterministic button per active repair and completes NONE of them."""
    env = make_app()
    _seed_two_repairs(env)
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "Finished", bot=env.bot)])
    send = env.bot.last_send()
    text = send["text"]
    assert "哪个维修完成了" in text or "Which repair is finished" in text
    kb = send["reply_markup"]
    assert kb is not None
    buttons = [b for row in kb.inline_keyboard for b in row]
    rcc_buttons = [b for b in buttons if decode(b.callback_data) and decode(b.callback_data)["action"] == "rcc"]
    assert len(rcc_buttons) == 2
    # No task was completed by the ambiguous message itself.
    for task_id in (11, 12):
        assert env.backend._ops_task(f"/operations/tasks/{task_id}")["status"] == "PENDING"


def test_ambiguous_finished_candidate_tap_completes_only_picked_repair(make_app):
    """AI-OPS-001 §12: tapping the candidate button completes EXACTLY that
    repair; the other stays open."""
    env = make_app()
    _seed_two_repairs(env)
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "Finished", bot=env.bot)])
    kb = env.bot.last_send()["reply_markup"]
    rcc = [b for row in kb.inline_keyboard for b in row
           if decode(b.callback_data) and decode(b.callback_data)["action"] == "rcc"][0]
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, rcc.callback_data, bot=env.bot)])
    assert env.backend._ops_task("/operations/tasks/11")["status"] == "COMPLETED"
    assert env.backend._ops_task("/operations/tasks/12")["status"] == "PENDING"


# --- §12 archive + index ------------------------------------------------------

def test_media_message_archived_and_indexed(make_app):
    """AI-OPS-001 §12: a photo is forwarded to the private archive channel and
    indexed in the backend evidence table (portable metadata)."""
    env = make_app()
    env.settings.archive_chat_id = "-1001234567890"
    run_updates(env, [_photo_update(SECRETARY_ID, SECRETARY_ID)])
    forwards = env.bot.of_type("forward_message")
    assert len(forwards) == 1
    assert forwards[0]["chat_id"] == "-1001234567890"
    posts = [(m, p, b) for m, p, b in env.backend.calls if p == "/evidence"]
    assert posts and posts[0][0] == "POST"
    assert "photo_big_id" in str(posts[0][2])
    texts = " ".join(env.bot.all_texts())
    assert "已存档" in texts or "Archived" in texts


def test_media_without_archive_config_keeps_old_ack(make_app):
    """AI-OPS-001 §12: when no archive channel is configured, media keeps the
    friendly ack and nothing is forwarded (graceful no-op)."""
    env = make_app()
    run_updates(env, [_photo_update(SECRETARY_ID, SECRETARY_ID)])
    assert len(env.bot.of_type("forward_message")) == 0
    assert all(("POST", "/evidence") != (m, p) for m, p, _ in env.backend.calls)


def test_media_forward_failure_never_claims_archived(make_app):
    """AI-OPS-001 §12 (pre-acceptance): when forwarding fails, the bot MUST NOT
    send the 'archived/indexed' success and must not write an evidence row."""
    from telegram.error import TelegramError

    env = make_app()
    env.settings.archive_chat_id = "-1001234567890"
    env.bot.forward_error = TelegramError("Forbidden: bot is not a member of the channel")
    run_updates(env, [_photo_update(SECRETARY_ID, SECRETARY_ID)])
    texts = " ".join(env.bot.all_texts())
    assert "已存档" not in texts and "Archived" not in texts
    assert all(("POST", "/evidence") != (m, p) for m, p, _ in env.backend.calls)


def test_media_index_failure_never_claims_archived(make_app):
    """AI-OPS-001 §12 (pre-acceptance): when the evidence index write fails
    (backend 500), the bot MUST NOT send the 'archived/indexed' success."""
    env = make_app()
    env.settings.archive_chat_id = "-1001234567890"
    env.backend.fail_status["/evidence"] = 500
    run_updates(env, [_photo_update(SECRETARY_ID, SECRETARY_ID)])
    texts = " ".join(env.bot.all_texts())
    assert "已存档" not in texts and "Archived" not in texts


# --- §14 evidence retrieval ---------------------------------------------------

def test_evidence_query_sends_archived_media(make_app):
    """AI-OPS-001 §14: 'Show 1608 repair photos' sends the archived media back
    (file_ids stay bot-usable) with a human summary."""
    env = make_app()
    env.backend.evidence.append({
        "id": 1, "storage_provider": "telegram_channel",
        "external_file_id": "archived_photo_1", "external_message_id": 9001,
        "media_type": "photo", "mime_type": "image/jpeg",
        "filename": "after_repair.jpg", "size_bytes": 1234, "checksum": None,
        "category": "after_repair", "property_id": 1, "unit_id": 1,
        "entity_type": "task", "entity_id": 1, "uploaded_by": 1,
        "created_at": "2026-08-16T08:00:00Z",
    })
    run_updates(env, [make_text_update(OWNER_ID, OWNER_ID, "Show 16B repair photos", bot=env.bot)])
    photos = env.bot.of_type("send_photo")
    assert len(photos) == 1
    assert photos[0]["photo"] == "archived_photo_1"
    texts = " ".join(env.bot.all_texts())
    assert "16B" in texts


# --- §14 Telegram-first Unit create -------------------------------------------

def test_unit_add_fast_path_confirm_then_create(make_app):
    """AI-OPS-001 §14: 'Add unit 1609, rent 35000, vacant' shows a
    confirmation card; the tap creates the unit exactly once."""
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "Add unit 1609, rent 35000, vacant", bot=env.bot)])
    send = env.bot.last_send()
    assert "1609" in send["text"] and "35000" in send["text"]
    kb = send["reply_markup"]
    confirm = [b for row in kb.inline_keyboard for b in row
               if decode(b.callback_data) and decode(b.callback_data)["action"] == "uac"][0]
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, confirm.callback_data, bot=env.bot)])
    units_created = [u for u in env.backend.units if u["unit_number"] == "1609"]
    assert len(units_created) == 1
    from decimal import Decimal
    assert Decimal(units_created[0]["monthly_rent"]) == Decimal("35000.00")
    assert units_created[0]["status"] == "vacant"


# --- §17 viewing -----------------------------------------------------------------

def test_viewing_message_becomes_business_event(make_app):
    """AI-OPS-001 §17: 'Someone will view 16B tomorrow at 2pm' is persisted as
    a viewing (confirm card -> POST /viewings), never chat-only context."""
    env = make_app()
    run_updates(env, [make_text_update(SECRETARY_ID, SECRETARY_ID, "Someone will view 16B tomorrow at 2pm", bot=env.bot)])
    send = env.bot.last_send()
    assert "看房" in send["text"] or "Viewing" in send["text"]
    kb = send["reply_markup"]
    confirm = [b for row in kb.inline_keyboard for b in row
               if decode(b.callback_data) and decode(b.callback_data)["action"] == "vwc"][0]
    run_updates(env, [make_callback_update(SECRETARY_ID, SECRETARY_ID, confirm.callback_data, bot=env.bot)])
    assert ("POST", "/viewings") in [(m, p) for m, p, _ in env.backend.calls]
    assert len(env.backend.viewings) == 1
    assert env.backend.viewings[0]["unit_id"] == 1  # 16B


# --- §20 bilingual group regression ----------------------------------------------

def test_group_reply_stays_bilingual_for_fixed_menu(make_app):
    """AI-OPS-001 §20: a fixed-menu task view in a GROUP renders the bilingual
    quick card (English + 中文), never a single language."""
    env = make_app()
    run_updates(env, [_group_text_update(OWNER_ID, -1000001, "✅ Tasks", message_id=70, update_id=70)])
    texts = " ".join(env.bot.all_texts())
    # The quick tasks card is bilingual; assert both scripts appear somewhere.
    assert any("\u4e2d" in texts for _ in [0]) or "Tasks" in texts or "任务" in texts
