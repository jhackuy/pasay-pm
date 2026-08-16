#!/usr/bin/env python3
"""AI-OPS-FOUNDATION-001 pre-acceptance: REAL Telegram Archive smoke.

Deterministic, no LLM. Proves the full archive path against the LIVE Windows
test Runtime:

1. Bot identity + permission on the archive channel (getMe / getChatMember).
2. Bot posts real media to the archive channel (sendPhoto multipart) ->
   Telegram returns message_id + file_id (archive message exists).
3. The evidence row is written through the LIVE Pasay API (POST /evidence)
   and carries external_file_id + external_message_id (authoritative index).
4. Retrieval: the bot sends the archived media back by its stored file_id
   (sendPhoto to the Owner DM) — proving the file_id is reusable.

Usage (from repo root, secrets never printed):
  set PASSAY_ARCHIVE_CHAT_ID=<id> then:
  .venv/Scripts/python bin/smoke_archive.py [--archive-id -100...]

Exit code 0 == SMOKE_PASS; any non-zero == SMOKE_FAIL (never claims success).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

import httpx

RUNTIME = r"D:\AI-Review\pasay-pm\worktrees\BOT-V1-USABLE-001-RUNTIME"
API_BASE = os.environ.get("PASAY_SMOKE_API_BASE", "http://127.0.0.1:8001/api/v1")

# 1x1 transparent PNG.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _load_env(path: str) -> dict:
    data = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def fail(msg: str) -> int:
    print(f"SMOKE_FAIL: {msg}")
    return 1


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    parser = argparse.ArgumentParser(prog="smoke_archive")
    parser.add_argument("--archive-id", default=None,
                        help="override PASSAY_ARCHIVE_CHAT_ID (e.g. -100…)")
    args = parser.parse_args(argv)

    root_env = _load_env(os.path.join(RUNTIME, ".env"))
    bot_env = _load_env(os.path.join(RUNTIME, "pasay-telegram-bot", ".env"))
    token = bot_env.get("PASSAY_TG_BOT_TOKEN") or root_env.get("TELEGRAM_BOT_TOKEN")
    admin_key = bot_env.get("PASSAY_ADMIN_API_KEY") or root_env.get("PASSAY_ADMIN_API_KEY")
    archive_id = args.archive_id or bot_env.get("PASSAY_ARCHIVE_CHAT_ID") or root_env.get("PASSAY_ARCHIVE_CHAT_ID")
    if not token:
        return fail("bot token missing")
    if not admin_key:
        return fail("admin API key missing")
    if not archive_id or not archive_id.strip():
        return fail("PASSAY_ARCHIVE_CHAT_ID is not configured")
    archive_id = archive_id.strip()

    tg = httpx.Client(base_url="https://api.telegram.org", timeout=30)
    api = httpx.Client(base_url=API_BASE, timeout=30)

    # 1. bot identity
    r = tg.post(f"/bot{token}/getMe")
    if r.status_code != 200 or not r.json().get("ok"):
        return fail(f"getMe failed: {r.status_code}")
    bot_id = r.json()["result"]["id"]

    # 2. archive permission
    r = tg.post(f"/bot{token}/getChat", json={"chat_id": archive_id})
    if r.status_code != 200 or not r.json().get("ok"):
        return fail(f"getChat(archive) failed: {r.status_code} {r.text[:200]}")
    chat = r.json()["result"]
    print(f"[smoke] archive chat type={chat.get('type')} title={chat.get('title')!r}")
    if chat.get("type") != "channel":
        return fail(f"archive chat is a {chat.get('type')}, not a channel")

    r = tg.post(f"/bot{token}/getChatMember", json={"chat_id": archive_id, "user_id": bot_id})
    if r.status_code != 200 or not r.json().get("ok"):
        return fail(f"getChatMember failed: {r.status_code} {r.text[:200]}")
    member = r.json()["result"]
    status = member.get("status")
    print(f"[smoke] bot archive membership status={status}")
    if status not in ("administrator", "member", "creator"):
        return fail(f"bot is not a post-capable member of the archive (status={status})")

    # 3. post real media to the archive (multipart) -> message exists
    files = {"photo": ("smoke.png", _TINY_PNG, "image/png")}
    data = {"chat_id": archive_id, "caption": "archive smoke probe"}
    r = tg.post(f"/bot{token}/sendPhoto", data=data, files=files)
    if r.status_code != 200 or not r.json().get("ok"):
        return fail(f"sendPhoto to archive failed: {r.status_code} {r.text[:300]}")
    sent = r.json()["result"]
    archive_message_id = sent.get("message_id")
    photos = sent.get("photo") or []
    file_id = photos[-1].get("file_id") if photos else None
    if not archive_message_id or not file_id:
        return fail("sendPhoto returned no message_id/file_id")
    print(f"[smoke] archive message exists: message_id={archive_message_id} file_id_present={bool(file_id)}")

    # 4. write the evidence row through the LIVE API (authoritative index)
    evidence_payload = {
        "storage_provider": "telegram_channel",
        "external_file_id": file_id,
        "external_message_id": archive_message_id,
        "media_type": "photo",
        "mime_type": "image/png",
        "filename": "smoke.png",
        "category": "other",
    }
    r = api.post(
        "/evidence",
        json=evidence_payload,
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    if r.status_code not in (200, 201):
        return fail(f"POST /evidence failed: {r.status_code} {r.text[:300]}")
    row = r.json()
    if not row.get("id"):
        return fail("POST /evidence returned no row id")
    print(f"[smoke] evidence row id={row['id']} external_file_id_set={bool(row.get('external_file_id'))} "
          f"external_message_id={row.get('external_message_id')}")

    # 5. retrieval: send the archived media back by its stored file_id
    owner_dm = os.environ.get("PASAY_SMOKE_RETRIEVAL_CHAT", "5177241442")
    r = tg.post(f"/bot{token}/sendPhoto", json={"chat_id": owner_dm, "photo": row["external_file_id"]})
    if r.status_code != 200 or not r.json().get("ok"):
        return fail(f"retrieval sendPhoto failed: {r.status_code} {r.text[:300]}")
    print(f"[smoke] retrieval sent media back to {owner_dm}")

    tg.close()
    api.close()
    print("SMOKE_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
