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

from pasay_bot.render import html as H
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
ACTION_EDIT = "ed"
# V1.2 operations center (待办中心).
ACTION_OPS_NAV = "opn"
ACTION_TASK_COMPLETE = "tkc"
ACTION_TASK_SNOOZE = "tks"
ACTION_TASK_SNOOZE_PICK = "tsp"
ACTION_TASK_DETAIL = "tkd"

# ops center section entities (callback entity field).
OPS_OVERVIEW = "ops"
OPS_SECTION_OVERDUE = "oov"
OPS_SECTION_TODAY = "otd"
OPS_SECTION_NEXT7 = "on7"
OPS_SECTION_ALL = "oal"

# bot-side snooze preset codes -> backend preset names.
SNOOZE_PRESET_MAP = {
    "1h": "1h",
    "today": "today_afternoon",
    "tomorrow": "tomorrow_morning",
    "3d": "3d",
}

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


def dashboard_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Home dashboard buttons (B1): rent, to-do, properties, finance + V1.2 待办中心."""
    kb = [
        [
            InlineKeyboardButton(
                t("nav.rent", locale), callback_data=encode(ACTION_NAV, "rent")
            ),
            InlineKeyboardButton(
                t("nav.pending", locale), callback_data=encode(ACTION_NAV, "pending")
            ),
        ],
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
                t("nav.ops", locale),
                callback_data=encode(ACTION_OPS_NAV, OPS_OVERVIEW),
            ),
        ],
    ]
    return InlineKeyboardMarkup(kb)


def menu_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Backward-compatible alias: the menu IS the home dashboard now."""
    return dashboard_keyboard(locale)


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
    unit_id: int,
    action: Optional[str] = None,
    locale: str = "zh",
    back_entity: str = "home",
    ref: str = "",
) -> InlineKeyboardMarkup:
    """State-driven unit-page buttons (B5):
    - action='collect' -> unpaid: [✅ 登记收租]
    - action='reopen'  -> reversed: [🔄 重新登记]
    - action='view'    -> paid:     [💰 查看付款] (ref = income id)
    - action=None      -> vacant / no active lease: no collect button
    """
    buttons = []
    if action == "collect":
        buttons.append(
            [
                InlineKeyboardButton(
                    t("rent.register_unpaid", locale),
                    callback_data=encode(ACTION_RENT, "go", str(unit_id)),
                )
            ]
        )
    elif action == "reopen":
        buttons.append(
            [
                InlineKeyboardButton(
                    t("rent.re_register", locale),
                    callback_data=encode(ACTION_RENT, "go", str(unit_id)),
                )
            ]
        )
    elif action == "view" and ref.isdigit():
        buttons.append(
            [
                InlineKeyboardButton(
                    t("rent.view_payment", locale),
                    callback_data=encode(ACTION_DETAIL, "inc", ref),
                )
            ]
        )
    label = t("common.home", locale) if back_entity == "home" else t("rent.back", locale)
    buttons.append(
        [InlineKeyboardButton(label, callback_data=encode(ACTION_NAV, back_entity))]
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
    nonce: str, ts: int, can_confirm: bool, locale: str = "zh",
    edit_available: bool = True,
) -> InlineKeyboardMarkup:
    label = t("rent.confirm", locale) if can_confirm else t("rent.confirm_pending", locale)
    kb = [
        [
            InlineKeyboardButton(
                label, callback_data=encode(ACTION_CONFIRM, "ren", nonce=nonce, ts=ts)
            ),
            InlineKeyboardButton(
                t("rent.edit_title", locale),
                callback_data=encode(ACTION_EDIT, "menu"),
            ),
        ],
        [
            InlineKeyboardButton(
                t("rent.cancel", locale), callback_data=encode(ACTION_CANCEL)
            ),
        ],
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
    buttons.append(
        [InlineKeyboardButton(t("common.home", locale), callback_data=encode(ACTION_NAV, "home"))]
    )
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


# --- V1.1 UX keyboards ---

def collect_list_keyboard(rows: list[dict], locale: str = "zh") -> InlineKeyboardMarkup:
    """Unpaid-unit collect list (B4). One button per unit -> direct entry."""
    buttons = []
    has_overdue = any(int(r.get("overdue_days") or 0) > 0 for r in rows)
    for r in rows:
        label = f"{r.get('unit_number', '')} · {H.money(r.get('amount'))}"
        buttons.append(
            [
                InlineKeyboardButton(
                    label, callback_data=encode(ACTION_RENT, "go", str(r["unit_id"]))
                )
            ]
        )
    if has_overdue:
        buttons.append(
            [
                InlineKeyboardButton(
                    t("pending.view_all_overdue", locale),
                    callback_data=encode(ACTION_NAV, "overdue"),
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(t("common.home", locale), callback_data=encode(ACTION_NAV, "home"))]
    )
    return InlineKeyboardMarkup(buttons)


def edit_menu_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Rent edit menu (B4: [✏️修改] -> amount / date / method / back)."""
    kb = [
        [
            InlineKeyboardButton(
                t("rent.edit_amount", locale), callback_data=encode(ACTION_EDIT, "amount")
            ),
            InlineKeyboardButton(
                t("rent.edit_date", locale), callback_data=encode(ACTION_EDIT, "date")
            ),
        ],
        [
            InlineKeyboardButton(
                t("rent.edit_method", locale), callback_data=encode(ACTION_EDIT, "method")
            ),
        ],
        [
            InlineKeyboardButton(
                t("rent.edit_back", locale), callback_data=encode(ACTION_EDIT, "back")
            ),
        ],
    ]
    return InlineKeyboardMarkup(kb)


def home_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Single [🏠 首页] — used by empty states, cancel and expired cards."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("common.home", locale), callback_data=encode(ACTION_NAV, "home"))]]
    )


def expired_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    return home_keyboard(locale)


def error_keyboard(entity: str, locale: str = "zh") -> InlineKeyboardMarkup:
    """Recovery buttons for load errors (B8): retry same page + home."""
    kb = [
        [
            InlineKeyboardButton(
                t("common.retry", locale), callback_data=encode(ACTION_NAV, entity)
            ),
            InlineKeyboardButton(
                t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
            ),
        ]
    ]
    return InlineKeyboardMarkup(kb)


def retry_confirm_keyboard(
    nonce: str, ts: int, locale: str = "zh", entity: str = "ren", ref: str = ""
) -> InlineKeyboardMarkup:
    """Retry a failed financial write with the SAME nonce (idempotency-safe)."""
    kb = [
        [
            InlineKeyboardButton(
                t("common.retry", locale),
                callback_data=encode(ACTION_CONFIRM, entity, ref, nonce=nonce, ts=ts),
            ),
            InlineKeyboardButton(
                t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
            ),
        ]
    ]
    return InlineKeyboardMarkup(kb)


def pending_page_keyboard(
    overdue_rows: list[dict],
    confirm_entries: list[tuple[int, str]],
    locale: str = "zh",
    can_confirm: bool = False,
) -> InlineKeyboardMarkup:
    """Aggregated to-do page buttons (B2): per-overdue-unit collect, per-
    pending-income confirm (OWNER only), view-all, home."""
    buttons = []
    for r in overdue_rows:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"✅ {r.get('unit', '')} · {H.money(r.get('total_outstanding'))}",
                    callback_data=encode(ACTION_RENT, "go", str(r["unit_id"])),
                )
            ]
        )
    if can_confirm:
        for income_id, label in confirm_entries:
            buttons.append(
                [
                    InlineKeyboardButton(
                        label,
                        callback_data=encode(
                            ACTION_CONFIRM, "inc", str(income_id),
                            nonce=new_nonce(), ts=now_ts(),
                        ),
                    )
                ]
            )
    if overdue_rows:
        buttons.append(
            [
                InlineKeyboardButton(
                    t("pending.view_all_overdue", locale),
                    callback_data=encode(ACTION_NAV, "overdue"),
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(t("common.home", locale), callback_data=encode(ACTION_NAV, "home"))]
    )
    return InlineKeyboardMarkup(buttons)


def edit_input_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Prompt keyboard during the [✏️修改] amount/date steps (B7)."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("rent.cancel", locale), callback_data=encode(ACTION_CANCEL))],
            [InlineKeyboardButton(t("common.home", locale), callback_data=encode(ACTION_NAV, "home"))],
        ]
    )


def edit_date_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Date-edit prompt: [📅 今天] shortcut + cancel + home (B4/B7)."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("rent.today", locale), callback_data=encode(ACTION_EDIT, "today"))],
            [InlineKeyboardButton(t("rent.cancel", locale), callback_data=encode(ACTION_CANCEL))],
            [InlineKeyboardButton(t("common.home", locale), callback_data=encode(ACTION_NAV, "home"))],
        ]
    )


# --- V1.2 operations center (待办中心) ------------------------------------

def ops_overview_keyboard(summary: dict, locale: str = "zh") -> InlineKeyboardMarkup:
    """待办中心 overview: four section buttons with counts."""
    kb = [
        [
            InlineKeyboardButton(
                f"🔴 {t('ops.section_overdue', locale)} · {int(summary.get('overdue', 0))}",
                callback_data=encode(ACTION_OPS_NAV, OPS_SECTION_OVERDUE),
            )
        ],
        [
            InlineKeyboardButton(
                f"🟠 {t('ops.section_today', locale)} · {int(summary.get('due_today', 0))}",
                callback_data=encode(ACTION_OPS_NAV, OPS_SECTION_TODAY),
            )
        ],
        [
            InlineKeyboardButton(
                f"🟡 {t('ops.section_next7', locale)} · {int(summary.get('due_7_days', 0))}",
                callback_data=encode(ACTION_OPS_NAV, OPS_SECTION_NEXT7),
            )
        ],
        [
            InlineKeyboardButton(
                f"📅 {t('ops.section_all', locale)} · {int(summary.get('pending_total', 0))}",
                callback_data=encode(ACTION_OPS_NAV, OPS_SECTION_ALL),
            )
        ],
        [
            InlineKeyboardButton(
                t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
            )
        ],
    ]
    return InlineKeyboardMarkup(kb)


def ops_section_keyboard(tasks: list, locale: str = "zh") -> InlineKeyboardMarkup:
    """Per-task rows: ✅ 完成 / ⏰ 稍后提醒 / 👁 查看详情 + ◀️ 返回."""
    kb = []
    for task in tasks:
        nonce, ts = new_nonce(), now_ts()
        kb.append(
            [
                InlineKeyboardButton(
                    t("ops.complete", locale),
                    callback_data=encode(ACTION_TASK_COMPLETE, "ops", str(task.id), nonce=nonce, ts=ts),
                ),
                InlineKeyboardButton(
                    t("ops.snooze", locale),
                    callback_data=encode(ACTION_TASK_SNOOZE, "ops", str(task.id)),
                ),
                InlineKeyboardButton(
                    t("ops.detail", locale),
                    callback_data=encode(ACTION_TASK_DETAIL, "ops", str(task.id)),
                ),
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                t("ops.back", locale), callback_data=encode(ACTION_OPS_NAV, OPS_OVERVIEW)
            )
        ]
    )
    return InlineKeyboardMarkup(kb)


def task_action_keyboard(task_id: int, locale: str = "zh") -> InlineKeyboardMarkup:
    """Detail card actions: ✅ 完成 / ⏰ 稍后提醒 / ◀️ 返回."""
    nonce, ts = new_nonce(), now_ts()
    kb = [
        [
            InlineKeyboardButton(
                t("ops.complete", locale),
                callback_data=encode(ACTION_TASK_COMPLETE, "ops", str(task_id), nonce=nonce, ts=ts),
            ),
            InlineKeyboardButton(
                t("ops.snooze", locale),
                callback_data=encode(ACTION_TASK_SNOOZE, "ops", str(task_id)),
            ),
        ],
        [
            InlineKeyboardButton(
                t("ops.back", locale), callback_data=encode(ACTION_OPS_NAV, OPS_OVERVIEW)
            )
        ],
    ]
    return InlineKeyboardMarkup(kb)


def snooze_preset_keyboard(task_id: int, locale: str = "zh") -> InlineKeyboardMarkup:
    """Snooze presets: 1 小时 / 今天下午 / 明天上午 / 3 天后 / 自定义."""
    kb = [
        [
            InlineKeyboardButton(
                t("ops.snooze_1h", locale),
                callback_data=encode(ACTION_TASK_SNOOZE_PICK, "1h", str(task_id), nonce=new_nonce(), ts=now_ts()),
            ),
            InlineKeyboardButton(
                t("ops.snooze_today", locale),
                callback_data=encode(ACTION_TASK_SNOOZE_PICK, "today", str(task_id), nonce=new_nonce(), ts=now_ts()),
            ),
        ],
        [
            InlineKeyboardButton(
                t("ops.snooze_tomorrow", locale),
                callback_data=encode(ACTION_TASK_SNOOZE_PICK, "tomorrow", str(task_id), nonce=new_nonce(), ts=now_ts()),
            ),
            InlineKeyboardButton(
                t("ops.snooze_3d", locale),
                callback_data=encode(ACTION_TASK_SNOOZE_PICK, "3d", str(task_id), nonce=new_nonce(), ts=now_ts()),
            ),
        ],
        [
            InlineKeyboardButton(
                t("ops.snooze_custom", locale),
                callback_data=encode(ACTION_TASK_SNOOZE_PICK, "custom", str(task_id)),
            ),
        ],
        [
            InlineKeyboardButton(
                t("ops.back", locale), callback_data=encode(ACTION_OPS_NAV, OPS_OVERVIEW)
            )
        ],
    ]
    return InlineKeyboardMarkup(kb)


def ops_back_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Small ◀️ 返回 (to 待办中心 overview) — used after complete/snooze."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("ops.back", locale), callback_data=encode(ACTION_OPS_NAV, OPS_OVERVIEW)
                )
            ]
        ]
    )
