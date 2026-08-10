"""InlineKeyboard construction + callback_data encode/decode (single source of truth).

Format: ``v1:<action>:<entity>:<ref>:<nonce>:<ts>``
- lowercase ASCII + digits + ':', <= 64 bytes (Telegram hard limit)
- trailing empty fields are trimmed
- no JSON, no base64, no Chinese, no PII
"""
from __future__ import annotations

import re
import secrets
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from pasay_bot.render.i18n import t

VERSION = "v1"
MAX_CALLBACK_BYTES = 64

ACTION_NAV = "nav"
ACTION_PAGE = "pg"
ACTION_RENT = "rn"
ACTION_METHOD = "mt"
ACTION_CONFIRM = "cnf"
ACTION_REVERSE = "rv"
ACTION_CANCEL = "ccl"
ACTION_DETAIL = "det"

METHODS = ["bank", "gcash", "cash", "other"]
METHOD_LABELS = {"bank": "Bank", "gcash": "GCash", "cash": "Cash", "other": "Other"}

_SAFE = re.compile(r"^[a-z0-9:]+$")
_NONCE = re.compile(r"[0-9a-f]{1,16}")


def now_ts() -> int:
    return int(time.time())


def new_nonce() -> str:
    return secrets.token_hex(4)  # 8 hex chars


def encode(
    action: str,
    entity: str = "",
    ref: str = "",
    nonce: str = "",
    ts: Optional[int] = None,
) -> str:
    parts = [VERSION, action, entity, ref, nonce]
    if ts is not None:
        parts.append(str(ts))
    while len(parts) > 2 and not parts[-1]:
        parts.pop()
    data = ":".join(parts)
    size = len(data.encode("ascii"))
    if size > MAX_CALLBACK_BYTES:
        raise ValueError(
            f"callback_data too long ({size} bytes > {MAX_CALLBACK_BYTES}): {data}"
        )
    return data


def decode(data: str) -> Optional[dict]:
    """Returns a dict or None for unknown versions / malformed data."""
    if not isinstance(data, str) or not data:
        return None
    if not _SAFE.match(data):
        return None
    parts = data.split(":")
    if parts[0] != VERSION or len(parts) < 2 or not parts[1]:
        return None
    action = parts[1]
    entity = parts[2] if len(parts) > 2 else ""
    ref = parts[3] if len(parts) > 3 else ""
    nonce = parts[4] if len(parts) > 4 else ""
    ts = parts[5] if len(parts) > 5 else ""
    if ref and not ref.isdigit():
        return None
    if nonce and not _NONCE.fullmatch(nonce):
        return None
    if ts and not ts.isdigit():
        return None
    return {
        "action": action,
        "entity": entity,
        "ref": ref,
        "nonce": nonce,
        "ts": int(ts) if ts else None,
    }


# --- keyboard builders ---
def _pagination_keyboard(
    action: str, entity: str, page: int, total_pages: int, locale: str
) -> InlineKeyboardMarkup:
    buttons = []
    if page > 1:
        buttons.append(
            InlineKeyboardButton(
                t("page.prev", locale),
                callback_data=encode(action, entity, str(page - 1)),
            )
        )
    if page < total_pages:
        buttons.append(
            InlineKeyboardButton(
                t("page.next", locale),
                callback_data=encode(action, entity, str(page + 1)),
            )
        )
    return InlineKeyboardMarkup([buttons])


def property_pagination_keyboard(page: int, total_pages: int, locale: str = "zh"):
    return _pagination_keyboard(ACTION_PAGE, "prop", page, total_pages, locale)


def overdue_pagination_keyboard(page: int, total_pages: int, locale: str = "zh"):
    return _pagination_keyboard(ACTION_PAGE, "ovd", page, total_pages, locale)


def menu_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(
                t("nav.properties", locale),
                callback_data=encode(ACTION_NAV, "properties"),
            ),
            InlineKeyboardButton(
                t("nav.finance", locale),
                callback_data=encode(ACTION_NAV, "finance"),
            ),
        ],
        [
            InlineKeyboardButton(
                t("nav.overdue", locale),
                callback_data=encode(ACTION_NAV, "overdue"),
            ),
            InlineKeyboardButton(
                t("nav.rent", locale),
                callback_data=encode(ACTION_NAV, "rent"),
            ),
        ],
    ]
    return InlineKeyboardMarkup(kb)


def property_list_keyboard(properties, locale: str = "zh") -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                f"🏢 {p.name}",
                callback_data=encode(ACTION_RENT, "prop", str(p.id)),
            )
        ]
        for p in properties
    ]
    buttons.append(
        [InlineKeyboardButton(t("rent.cancel", locale), callback_data=encode(ACTION_CANCEL))]
    )
    return InlineKeyboardMarkup(buttons)


def unit_list_keyboard(units, locale: str = "zh") -> InlineKeyboardMarkup:
    buttons = []
    for u in units:
        mark = "🟢" if u.status == "occupied" else ("⚪" if u.status == "vacant" else "🔵")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{mark} {u.unit_number}",
                    callback_data=encode(ACTION_RENT, "unit", str(u.id)),
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(t("rent.cancel", locale), callback_data=encode(ACTION_CANCEL))]
    )
    return InlineKeyboardMarkup(buttons)


def unit_page_keyboard(
    unit_id: int, can_rent: bool, locale: str = "zh"
) -> InlineKeyboardMarkup:
    buttons = []
    if can_rent:
        buttons.append(
            [
                InlineKeyboardButton(
                    t("rent.register", locale),
                    callback_data=encode(ACTION_RENT, "go", str(unit_id)),
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(t("rent.back", locale), callback_data=encode(ACTION_NAV, "rent"))]
    )
    return InlineKeyboardMarkup(buttons)


def payment_method_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                METHOD_LABELS[m], callback_data=encode(ACTION_METHOD, m)
            )
        ]
        for m in METHODS
    ]
    buttons.append(
        [InlineKeyboardButton(t("rent.cancel", locale), callback_data=encode(ACTION_CANCEL))]
    )
    return InlineKeyboardMarkup(buttons)


def confirm_rent_keyboard(
    nonce: str, ts: int, can_confirm: bool, locale: str = "zh"
) -> InlineKeyboardMarkup:
    label = t("rent.confirm", locale) if can_confirm else t("rent.confirm_pending", locale)
    kb = [
        [
            InlineKeyboardButton(
                label, callback_data=encode(ACTION_CONFIRM, "ren", nonce=nonce, ts=ts)
            ),
            InlineKeyboardButton(
                t("rent.cancel", locale), callback_data=encode(ACTION_CANCEL)
            ),
        ]
    ]
    return InlineKeyboardMarkup(kb)


def confirm_income_keyboard(
    income_id: int, nonce: str, ts: int, can_reverse: bool, locale: str = "zh"
) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(
                t("rent.confirm", locale),
                callback_data=encode(ACTION_CONFIRM, "inc", str(income_id), nonce=nonce, ts=ts),
            )
        ]
    ]
    if can_reverse:
        kb.append(
            [
                InlineKeyboardButton(
                    t("rent.reverse", locale),
                    callback_data=encode(ACTION_REVERSE, "inc", str(income_id), nonce=nonce, ts=ts),
                )
            ]
        )
    return InlineKeyboardMarkup(kb)


def overdue_page_keyboard(rows, page: int, total_pages: int, locale: str = "zh"):
    """One message, per-item action buttons + pagination row."""
    buttons = []
    for row in rows:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"✅ {row.unit}",
                    callback_data=encode(ACTION_RENT, "unit", str(row.unit_id)),
                ),
                InlineKeyboardButton(
                    f"📄 {row.unit}",
                    callback_data=encode(ACTION_DETAIL, "unit", str(row.unit_id)),
                ),
            ]
        )
    pag = []
    if page > 1:
        pag.append(
            InlineKeyboardButton(
                t("page.prev", locale), callback_data=encode(ACTION_PAGE, "ovd", str(page - 1))
            )
        )
    if page < total_pages:
        pag.append(
            InlineKeyboardButton(
                t("page.next", locale), callback_data=encode(ACTION_PAGE, "ovd", str(page + 1))
            )
        )
    if pag:
        buttons.append(pag)
    return InlineKeyboardMarkup(buttons)


def pending_list_keyboard(entries, locale: str = "zh") -> InlineKeyboardMarkup:
    """One confirm button per pending income (OWNER /pending list, F5)."""
    buttons = [
        [
            InlineKeyboardButton(
                label,
                callback_data=encode(
                    ACTION_CONFIRM, "inc", str(income_id),
                    nonce=new_nonce(), ts=now_ts(),
                ),
            )
        ]
        for income_id, label in entries
    ]
    buttons.append(
        [InlineKeyboardButton(t("rent.cancel", locale), callback_data=encode(ACTION_CANCEL))]
    )
    return InlineKeyboardMarkup(buttons)
