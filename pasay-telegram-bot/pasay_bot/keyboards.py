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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from pasay_bot.render import html as H
from pasay_bot.render.i18n import bl as t
from pasay_bot.roles import Role

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
# V1.3 Slice 2 (Entry B, Secretary register): read-only "有问题" status hint.
ACTION_ISSUE = "iss"
# V1.3 Slice 2 (Entry D, rent status selector): multi-match candidate pick.
ACTION_RENT_STATUS_SELECT = "rss"
# P1-PASAY-NIGHTLY-PRODUCT-HARDENING-008 C: payment-history candidate pick.
ACTION_RENT_HISTORY_SELECT = "rhs"
# V1.3 Slice 1: expense approval (exa = approve, exr = reject, exd = detail).
ACTION_EXPENSE_APPROVE = "exa"
ACTION_EXPENSE_REJECT = "exr"
ACTION_EXPENSE_DETAIL = "exd"
# BOT-V1-USABLE-001 P0-2: expense create flow (submit for approval / edit).
ACTION_EXPENSE_CREATE = "exc"
ACTION_EXPENSE_EDIT = "exe"
# PASAY-V2-EXPENSE-PAYABLE-TASK-006: pay an APPROVED (unpaid) expense.
ACTION_EXPENSE_PAY = "exp"        # open the deterministic pay flow (confirm + warn)
ACTION_EXPENSE_PAY_CONFIRM = "expc"  # finalize payment (idempotent, backend-verified)
# BOT-V1-USABLE-001 P0-5: AI fallback ambiguity choices (deterministic taps).
ACTION_AI_CHOICE = "aic"
# BOT-V1-USABLE-001 home summary action buttons.
ACTION_HOME_NAV = "hnv"
# V1.2 operations center (待办中心).
ACTION_OPS_NAV = "opn"
ACTION_TASK_COMPLETE = "tkc"
ACTION_TASK_SNOOZE = "tks"
ACTION_TASK_SNOOZE_PICK = "tsp"
ACTION_TASK_DETAIL = "tkd"
# AI-OPS-FOUNDATION-001 §9/§12: ambiguous "finished" -> deterministic
# candidate pick (one repair per button; never guesses which task to close).
ACTION_REPAIR_COMPLETE_CANDIDATE = "rcc"
# AI-OPS-FOUNDATION-001 §14/§17: Telegram-first Unit CRUD + viewing confirms.
ACTION_UNIT_ADD_CONFIRM = "uac"
ACTION_VIEWING_CONFIRM = "vwc"
# C1.1 运营助手 (copilot).
ACTION_COPILOT_WHY = "cpw"
ACTION_COPILOT_ASK = "cpa"
ACTION_COPILOT_BACK = "cpb"
ACTION_COPILOT_NAV = "cpn"
# C1.2/C2 confirmed-action copilot (v1.2.2). Short ASCII action codes (the
# callback scheme only allows ``[a-z0-9:]``); full names in the comments.
ACTION_COPILOT_SUGGEST = "cps"          # cp_suggest (suggestion tap)
ACTION_COPILOT_CONFIRM = "cpc"          # cp_confirm ([✅ 确认安排] -> execute)
ACTION_COPILOT_DECLINE = "cpd"          # cp_decline ([暂不处理] -> cancel)
ACTION_COPILOT_EDIT = "cpe"             # cp_edit ([✏️ 修改] inline pick)
ACTION_COPILOT_RECOMMEND_BACK = "cpr"   # cp_recommend_back (return to TODAY)
ACTION_COPILOT_SNOOZE_PICK = "csp"      # cp_snooze_pick (edit due preset)
ACTION_COPILOT_ASSIGNEE_PICK = "cap"    # cp_assignee_pick (edit who)

# TELEGRAM-OPS-UX-CONVERGENCE-001: Quick View action buttons (deterministic,
# index-based so no internal unit/expense id ever travels in callback_data).
# ``qvv`` opens a unit Quick View from the Properties index (ref = 1-based row
# into the re-fetched quick-properties list); ``par`` deep-links the property
# archive channel; ``rnq`` opens the Rent detail card for one overdue row;
# ``rfu`` is a rent follow-up (entity = unit id); ``rmo`` reminds the Owner.
ACTION_QUICK_UNIT_VIEW = "qvv"
ACTION_PROP_ARCHIVE = "par"
ACTION_RENT_QUICK_DETAIL = "rnq"
ACTION_RENT_FOLLOWUP = "rfu"
ACTION_REMIND_OWNER = "rmo"

# --- Fixed bottom Reply Keyboard (single source of truth) -------------------
# Exact button label -> deterministic route. These labels are UI commands,
# NOT natural language: the text-message handler must exact-match them and
# route deterministically BEFORE any NL/NLU/LLM processing can run.
#
# PASAY-V2-FOUNDATION-001: V2 menu is 4 simple-English Quick View buttons
# shared by every role. They are direct views, never a feature navigation.
FIXED_MENU_ROUTES: dict[str, str] = {
    "🏠 Properties": "properties",
    "✅ Tasks": "tasks",
    "💰 Rent": "rent",
    "💸 Expense": "expense",
}

# V2 legacy aliases: old Chinese labels still route deterministically so
# keyboards already pinned on clients keep working after deploy. They are
# never part of the visible V2 menu.
LEGACY_MENU_ROUTES: dict[str, str] = {
    "🏠 首页": "home",
    "✅ 待办": "pending",
    "💰 收租": "rent",
    "💸 支出": "expense",
}

# Row layout for the persistent keyboard (role -> rows of exact labels).
_FIXED_REPLY_ROWS = [
    ["🏠 Properties", "✅ Tasks"],
    ["💰 Rent", "💸 Expense"],
]


def fixed_menu_route_for(text: str) -> Optional[str]:
    """Exact-match a fixed bottom-menu button label to its deterministic
    route. Returns None when the text is NOT a fixed button, so free text
    still falls through to the conversation/NL path unchanged.

    V2: the English labels are primary; legacy Chinese labels keep working as
    aliases for already-pinned keyboards (never shown in the V2 menu)."""
    normalized = (text or "").strip()
    route = FIXED_MENU_ROUTES.get(normalized)
    if route is not None:
        return route
    return LEGACY_MENU_ROUTES.get(normalized)


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


def rent_status_candidates_keyboard(
    candidates: list[dict],
    locale: str = "zh",
    nonce: str = "",
    ts: Optional[int] = None,
) -> InlineKeyboardMarkup:
    """Multi-match selector (V1.3 Slice 2, Entry D): one read-only inline
    button per candidate, labelled ``property · unit · tenant``. The callback
    carries only the 1-based row index + per-card nonce; the handler resolves
    the row from the stored selector state, so no internal id ever travels in
    callback_data or the label."""
    return _candidate_selector_keyboard(
        ACTION_RENT_STATUS_SELECT, candidates, locale, nonce, ts,
    )


def rent_history_candidates_keyboard(
    candidates: list[dict],
    locale: str = "zh",
    nonce: str = "",
    ts: Optional[int] = None,
) -> InlineKeyboardMarkup:
    """Multi-match selector for payment-history questions (P1-...-008 C):
    same shape as the rent-status selector, distinct action so the tap handler
    renders the history card for the chosen candidate."""
    return _candidate_selector_keyboard(
        ACTION_RENT_HISTORY_SELECT, candidates, locale, nonce, ts,
    )


def _candidate_selector_keyboard(
    action: str,
    candidates: list[dict],
    locale: str = "zh",
    nonce: str = "",
    ts: Optional[int] = None,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for i, c in enumerate(candidates, start=1):
        parts = [
            str(c.get("property_name") or "").strip(),
            str(c.get("unit_number") or "").strip(),
            str(c.get("tenant_name") or "").strip(),
        ]
        label = " · ".join(p for p in parts if p)
        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=encode(
                        action, "sel", str(i),
                        nonce=nonce, ts=ts,
                    ),
                )
            ]
        )
    return InlineKeyboardMarkup(buttons)


def dashboard_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Minimal fallback for the ☰ 更多 page (V1.3): infrequent actions that no
    longer live on the primary bottom navigation. The six-grid is gone; the
    persistent reply keyboard is the primary nav."""
    kb = [
        [
            InlineKeyboardButton(
                t("nav.rent", locale), callback_data=encode(ACTION_NAV, "rent")
            ),
            InlineKeyboardButton(
                t("nav.overdue", locale), callback_data=encode(ACTION_NAV, "overdue")
            ),
        ],
        [
            InlineKeyboardButton(
                t("nav.copilot", locale),
                callback_data=encode(ACTION_COPILOT_NAV, "today"),
            ),
            InlineKeyboardButton(
                t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
            ),
        ],
    ]
    return InlineKeyboardMarkup(kb)


def reply_keyboard(role) -> ReplyKeyboardMarkup:
    """Persistent bottom navigation (PASAY-V2-FOUNDATION-001): one identical
    4-button English Quick View menu for every role (Properties / Tasks /
    Rent / Expense). Every label is an exact-match UI command routed
    deterministically (see ``FIXED_MENU_ROUTES`` / ``fixed_menu_route_for``)
    and never reaches NL/NLU/LLM processing. ``is_persistent=True`` pins the
    keyboard above the input field across messages."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label) for label in row] for row in _FIXED_REPLY_ROWS],
        resize_keyboard=True,
        is_persistent=True,
    )


def copilot_today_keyboard(
    item_count: int, locale: str = "zh",
) -> InlineKeyboardMarkup:
    """Operate-assistant TODAY card buttons (C1.1 fast-first): a per-item
    [为什么?] button (index 1..N) plus [问运营助手] and [🏠 首页].

    The per-item button's callback entity is the 1-based index into the
    deterministic TODAY top-items; the handler re-fetches the fast TODAY to
    resolve ``item_ref``, avoiding encoding backend refs in callback_data.
    """
    kb: list[list[InlineKeyboardButton]] = []
    why_row = [
        InlineKeyboardButton(
            f"{i+1} {t('copilot.why_button', locale)}",
            callback_data=encode(ACTION_COPILOT_WHY, str(i + 1)),
        )
        for i in range(min(item_count, 3))
    ]
    if why_row:
        kb.append(why_row)
    kb.append(
        [
            InlineKeyboardButton(
                t("copilot.ask_button", locale),
                callback_data=encode(ACTION_COPILOT_ASK, "ask"),
            ),
            InlineKeyboardButton(
                t("common.home", locale),
                callback_data=encode(ACTION_NAV, "home"),
            ),
        ]
    )
    return InlineKeyboardMarkup(kb)


# --- C2 confirmed-action copilot (v1.2.2) -----------------------------------
# Follow-up proposals are only buildable for the backend allowlist sources.
_COPILOT_FOLLOWUP_SOURCES = frozenset({"lease", "property", "task"})


def _copilot_item_source(item) -> str:
    return (getattr(item, "item_ref", "") or "").split(":", 1)[0].lower()


def copilot_item_actionable(item) -> bool:
    """Suggestion rows only for actionable items: a ``suggested_action`` AND a
    follow-up-eligible source (lease/property/task — mirrors the backend
    allowlist; expense/settlement items are never actionable here)."""
    has_action = bool((getattr(item, "suggested_action", "") or "").strip())
    return has_action and _copilot_item_source(item) in _COPILOT_FOLLOWUP_SOURCES


def copilot_why_keyboard(
    item_index: int, item, locale: str = "zh", can_suggest: bool = False,
) -> InlineKeyboardMarkup:
    """WHY card buttons (C2): a per-item suggestion row for actionable items
    ([安排秘书跟进] always, [明天再提醒] for task items, [暂不处理] dismiss),
    plus back-to-TODAY and home. ``item_index`` is the 1-based TODAY index;
    the handler re-fetches TODAY to resolve the backend ref."""
    kb: list[list[InlineKeyboardButton]] = []
    if can_suggest and copilot_item_actionable(item):
        follow = InlineKeyboardButton(
            t("copilot.suggest_follow", locale),
            callback_data=encode(
                ACTION_COPILOT_SUGGEST, "follow", str(item_index),
                nonce=new_nonce(), ts=now_ts(),
            ),
        )
        if _copilot_item_source(item) == "task":
            kb.append(
                [
                    follow,
                    InlineKeyboardButton(
                        t("copilot.suggest_snooze", locale),
                        callback_data=encode(
                            ACTION_COPILOT_SUGGEST, "snooze", str(item_index),
                            nonce=new_nonce(), ts=now_ts(),
                        ),
                    ),
                ]
            )
        else:
            kb.append([follow])
        kb.append(
            [
                InlineKeyboardButton(
                    t("copilot.suggest_dismiss", locale),
                    callback_data=encode(
                        ACTION_COPILOT_SUGGEST, "dismiss", str(item_index),
                        nonce=new_nonce(), ts=now_ts(),
                    ),
                )
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                t("copilot.back_today", locale),
                callback_data=encode(ACTION_COPILOT_RECOMMEND_BACK, "today"),
            ),
            InlineKeyboardButton(
                t("common.home", locale),
                callback_data=encode(ACTION_NAV, "home"),
            ),
        ]
    )
    return InlineKeyboardMarkup(kb)


def copilot_confirm_keyboard(proposal_id: int, locale: str = "zh") -> InlineKeyboardMarkup:
    """Confirmation card: [✅ 确认安排] [✏️ 修改] [暂不处理]. The [✅] tap is the
    ONLY path that executes — every mutation needs this explicit tap."""
    nonce, ts = new_nonce(), now_ts()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("copilot.confirm_yes", locale),
                    callback_data=encode(
                        ACTION_COPILOT_CONFIRM, str(proposal_id), "0",
                        nonce=nonce, ts=ts,
                    ),
                ),
                InlineKeyboardButton(
                    t("copilot.confirm_edit", locale),
                    callback_data=encode(
                        ACTION_COPILOT_EDIT, "menu", str(proposal_id),
                        nonce=nonce, ts=ts,
                    ),
                ),
                InlineKeyboardButton(
                    t("copilot.suggest_dismiss", locale),
                    callback_data=encode(
                        ACTION_COPILOT_DECLINE, str(proposal_id), "0",
                        nonce=nonce, ts=ts,
                    ),
                ),
            ]
        ]
    )


def copilot_success_keyboard(task_id: Optional[int], locale: str = "zh") -> InlineKeyboardMarkup:
    """Success card: [查看任务] (ops detail) + [返回今日重点] + home."""
    kb: list[list[InlineKeyboardButton]] = []
    if task_id is not None:
        kb.append(
            [
                InlineKeyboardButton(
                    t("copilot.view_task", locale),
                    callback_data=encode(ACTION_TASK_DETAIL, "ops", str(task_id)),
                )
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                t("copilot.back_today", locale),
                callback_data=encode(ACTION_COPILOT_RECOMMEND_BACK, "today"),
            ),
            InlineKeyboardButton(
                t("common.home", locale),
                callback_data=encode(ACTION_NAV, "home"),
            ),
        ]
    )
    return InlineKeyboardMarkup(kb)


def copilot_back_today_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Replay / notify-retry cards: back to TODAY + home."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("copilot.back_today", locale),
                    callback_data=encode(ACTION_COPILOT_RECOMMEND_BACK, "today"),
                ),
                InlineKeyboardButton(
                    t("common.home", locale),
                    callback_data=encode(ACTION_NAV, "home"),
                ),
            ]
        ]
    )


def copilot_stale_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Target-changed failure: refresh the latest TODAY + home."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("copilot.refresh", locale),
                    callback_data=encode(ACTION_COPILOT_RECOMMEND_BACK, "today"),
                ),
                InlineKeyboardButton(
                    t("common.home", locale),
                    callback_data=encode(ACTION_NAV, "home"),
                ),
            ]
        ]
    )


def copilot_edit_menu_keyboard(
    proposal_id: int, is_snooze: bool, locale: str = "zh",
) -> InlineKeyboardMarkup:
    """[✏️ 修改] inline menu: who (followups only) + due + back to confirm."""
    nonce, ts = new_nonce(), now_ts()
    row = []
    if not is_snooze:
        row.append(
            InlineKeyboardButton(
                t("copilot.edit_who", locale),
                callback_data=encode(ACTION_COPILOT_EDIT, "who", str(proposal_id), nonce=nonce, ts=ts),
            )
        )
    row.append(
        InlineKeyboardButton(
            t("copilot.edit_due", locale),
            callback_data=encode(ACTION_COPILOT_EDIT, "due", str(proposal_id), nonce=nonce, ts=ts),
        )
    )
    kb = [row]
    kb.append(
        [
            InlineKeyboardButton(
                t("copilot.back_confirm", locale),
                callback_data=encode(ACTION_COPILOT_EDIT, "back", str(proposal_id), nonce=nonce, ts=ts),
            )
        ]
    )
    return InlineKeyboardMarkup(kb)


def copilot_who_keyboard(proposal_id: int, locale: str = "zh") -> InlineKeyboardMarkup:
    """Who picker: [秘书] (backend default) / [我自己] (owner assigns self)."""
    nonce, ts = new_nonce(), now_ts()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("copilot.who_secretary", locale),
                    callback_data=encode(ACTION_COPILOT_ASSIGNEE_PICK, "sec", str(proposal_id), nonce=nonce, ts=ts),
                ),
                InlineKeyboardButton(
                    t("copilot.who_me", locale),
                    callback_data=encode(ACTION_COPILOT_ASSIGNEE_PICK, "me", str(proposal_id), nonce=nonce, ts=ts),
                ),
            ],
            [
                InlineKeyboardButton(
                    t("copilot.back_confirm", locale),
                    callback_data=encode(ACTION_COPILOT_EDIT, "back", str(proposal_id), nonce=nonce, ts=ts),
                ),
            ],
        ]
    )


def copilot_due_keyboard(proposal_id: int, locale: str = "zh") -> InlineKeyboardMarkup:
    """Due picker: 今天下午 / 明天上午 / 3 天后 (exact time resolved backend-side
    and always shown on the resulting confirmation card)."""
    nonce, ts = new_nonce(), now_ts()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("copilot.due_today", locale),
                    callback_data=encode(ACTION_COPILOT_SNOOZE_PICK, "today", str(proposal_id), nonce=nonce, ts=ts),
                ),
                InlineKeyboardButton(
                    t("copilot.due_tomorrow", locale),
                    callback_data=encode(ACTION_COPILOT_SNOOZE_PICK, "tomorrow", str(proposal_id), nonce=nonce, ts=ts),
                ),
                InlineKeyboardButton(
                    t("copilot.due_3d", locale),
                    callback_data=encode(ACTION_COPILOT_SNOOZE_PICK, "3d", str(proposal_id), nonce=nonce, ts=ts),
                ),
            ],
            [
                InlineKeyboardButton(
                    t("copilot.back_confirm", locale),
                    callback_data=encode(ACTION_COPILOT_EDIT, "back", str(proposal_id), nonce=nonce, ts=ts),
                ),
            ],
        ]
    )


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


def rent_match_keyboard(
    nonce: str, ts: int, can_confirm: bool, locale: str = "zh"
) -> Optional[InlineKeyboardMarkup]:
    """Entry B exact-payment card: a single [✓ 确认入账] for the Owner; no
    buttons at all when the role cannot confirm (Secretary). Navigation stays
    on the persistent bottom keyboard."""
    if not can_confirm:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("rent.match_confirm", locale),
                    callback_data=encode(ACTION_CONFIRM, "ren", nonce=nonce, ts=ts),
                )
            ]
        ]
    )


def confirm_income_keyboard(
    income_id: int,
    nonce: str,
    ts: int,
    can_reverse: bool,
    locale: str = "zh",
    show_confirm: bool = True,
) -> InlineKeyboardMarkup:
    kb = []
    if show_confirm:
        kb.append(
            [
                InlineKeyboardButton(
                    t("rent.confirm", locale),
                    callback_data=encode(ACTION_CONFIRM, "inc", str(income_id), nonce=nonce, ts=ts),
                )
            ]
        )
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


def secretary_registered_keyboard(
    income_id: int, nonce: str, ts: int, locale: str = "zh",
) -> InlineKeyboardMarkup:
    """Owner confirmation card for a Secretary-registered pending rent payment
    (V1.3 Slice 2): [✓ 确认入账] reuses the Owner-only income confirm chain
    (entity ``inc``) and [有问题] is a read-only status hint, never a write."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("rent.match_confirm", locale),
                    callback_data=encode(
                        ACTION_CONFIRM, "inc", str(income_id),
                        nonce=nonce, ts=ts,
                    ),
                ),
                InlineKeyboardButton(
                    t("rent.issue_button", locale),
                    callback_data=encode(ACTION_ISSUE, "inc", str(income_id)),
                ),
            ]
        ]
    )


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
    """Collect list (B4 / P0-RENT-COLLECTION-UX-003). One DISTINCT button per
    unit (unit + outstanding) -> direct entry; never repeated identical
    generic labels."""
    buttons = []
    has_overdue = any(int(r.get("overdue_days") or 0) > 0 for r in rows)
    for r in rows:
        label = (
            f"{r.get('unit_number', '')} · "
            f"{t('rent.outstanding', locale)} {H.money(r.get('outstanding'))}"
        )
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


def repair_completion_candidates_keyboard(
    tasks: list, locale: str = "zh", nonce: str = "", ts: Optional[int] = None,
) -> InlineKeyboardMarkup:
    """AI-OPS-FOUNDATION-001 §9/§12: one deterministic button per active
    repair candidate when "finished" is ambiguous — the bot never guesses
    which task to close."""
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks:
        unit = (getattr(task, "property_code", None) or (task.details or {}).get("unit_number") or "")
        label = f"{unit} · {task.title}".strip(" · ")[:60]
        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=encode(
                    ACTION_REPAIR_COMPLETE_CANDIDATE, "r", str(task.id), nonce, ts,
                ),
            )
        ])
    rows.append([InlineKeyboardButton(t("rent.cancelled", locale), callback_data=encode(ACTION_CANCEL))])
    return InlineKeyboardMarkup(rows)


def unit_add_confirm_keyboard(nonce: str, ts: Optional[int], locale: str = "zh") -> InlineKeyboardMarkup:
    """AI-OPS-FOUNDATION-001 §14: confirmation card for the Telegram-first
    Unit create fast path (state-changing creation needs one explicit tap)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t("unit_add.confirm", locale), callback_data=encode(ACTION_UNIT_ADD_CONFIRM, "u", "1", nonce, ts)),
            InlineKeyboardButton(t("rent.cancelled", locale), callback_data=encode(ACTION_CANCEL)),
        ]
    ])


def viewing_confirm_keyboard(nonce: str, ts: Optional[int], locale: str = "zh") -> InlineKeyboardMarkup:
    """AI-OPS-FOUNDATION-001 §17: confirmation card for a detected viewing."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(t("viewing.confirm", locale), callback_data=encode(ACTION_VIEWING_CONFIRM, "v", "1", nonce, ts)),
            InlineKeyboardButton(t("rent.cancelled", locale), callback_data=encode(ACTION_CANCEL)),
        ]
    ])


# --- V1.3 Slice 1: expense approval action cards ---------------------------

def _compact_peso(value) -> str:
    """'₱3,500' or '₱3.5K' for large amounts (approval button label only)."""
    from decimal import Decimal as _D, InvalidOperation

    try:
        d = _D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        d = _D("0")
    if abs(d) >= _D("10000"):
        return _compact_k(d)
    return H.money(d)


def _compact_k(value) -> str:
    """'₱3.5K' compact form (>= ₱1,000), else normal peso formatting."""
    from decimal import Decimal as _D, InvalidOperation

    try:
        d = _D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        d = _D("0")
    if abs(d) >= _D("1000"):
        scaled = f"{abs(d) / _D('1000'):.1f}".rstrip("0").rstrip(".")
        return f"₱{scaled}K"
    return H.money(d)


def _approve_button_label(locale: str = "zh", amount=None, unit=None) -> str:
    """PASAY-V2-FOUNDATION-001: approval button carries the value.

    'Approve ₱3,500' or 'Approve 1680 · ₱3.5K' when an amount is known;
    otherwise falls back to the plain locale label (back-compat for callers
    that do not know the amount yet)."""
    if amount is None:
        return t("expense.approve", locale)
    if unit:
        return f"Approve {unit} · {_compact_k(amount)}"
    return f"Approve {_compact_peso(amount)}"


def _reject_button_label(locale: str = "zh", amount=None) -> str:
    """V2: the reject action stays plain English next to the amount-aware
    approve button; falls back to the locale label when no amount is known."""
    if amount is not None:
        return "Reject"
    return t("expense.reject", locale)


def expense_approval_keyboard(
    expense_id: int, locale: str = "zh", has_receipt: bool = False,
    amount=None, unit=None,
) -> InlineKeyboardMarkup:
    """Expense approval card: [Approve ₱3,500][Reject] + secondary buttons.

    V2: the approve button carries the amount (and unit when known) so the
    Owner decides with the value in front of them, e.g. ``Approve 1680 ·
    ₱3.5K``."""
    nonce, ts = new_nonce(), now_ts()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _approve_button_label(locale, amount=amount, unit=unit),
                    callback_data=encode(
                        ACTION_EXPENSE_APPROVE, str(expense_id), "", nonce=nonce, ts=ts
                    ),
                ),
                InlineKeyboardButton(
                    _reject_button_label(locale, amount=amount),
                    callback_data=encode(
                        ACTION_EXPENSE_REJECT, str(expense_id), "", nonce=nonce, ts=ts
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    t("expense.view_receipt", locale)
                    if has_receipt
                    else t("expense.view_detail", locale),
                    callback_data=encode(ACTION_EXPENSE_DETAIL, str(expense_id)),
                ),
                InlineKeyboardButton(
                    t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
                ),
            ],
        ]
    )


def expense_detail_keyboard(
    expense_id: int, *, still_pending: bool = False, locale: str = "zh",
    amount=None, unit=None,
) -> InlineKeyboardMarkup:
    """Detail card buttons: keep approve/reject while the expense is still
    pending (the handler re-checks the backend state before rendering)."""
    kb: list[list[InlineKeyboardButton]] = []
    if still_pending:
        nonce, ts = new_nonce(), now_ts()
        kb.append(
            [
                InlineKeyboardButton(
                    _approve_button_label(locale, amount=amount, unit=unit),
                    callback_data=encode(
                        ACTION_EXPENSE_APPROVE, str(expense_id), "", nonce=nonce, ts=ts
                    ),
                ),
                InlineKeyboardButton(
                    _reject_button_label(locale, amount=amount),
                    callback_data=encode(
                        ACTION_EXPENSE_REJECT, str(expense_id), "", nonce=nonce, ts=ts
                    ),
                ),
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
            )
        ]
    )
    return InlineKeyboardMarkup(kb)


def expense_result_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Result card (approved/rejected): back home; nothing else to do."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("common.home", locale), callback_data=encode(ACTION_NAV, "home"))]]
    )


def expense_pay_confirm_keyboard(
    expense_id: int,
    locale: str = "zh",
    *,
    similar: list | None = None,
) -> InlineKeyboardMarkup:
    """Payment confirmation card for an APPROVED (unpaid) expense
    (PASAY-V2-EXPENSE-PAYABLE-TASK-006 §4/§5/§7).

    Primary action is the deterministic ``✅ Confirm paid`` (calls the backend
    pay transition); a receipt is OPTIONAL and never blocks PAID. When
    ``similar`` carries possible-duplicate PAID rows the Owner is shown
    ``Continue`` / ``Cancel`` / ``View existing`` — the warning is advisory
    and never auto-rejects the current expense."""
    nonce, ts = new_nonce(), now_ts()
    kb: list[list[InlineKeyboardButton]] = []
    if similar:
        kb.append(
            [
                InlineKeyboardButton(
                    t("expense.pay_duplicate_continue", locale),
                    callback_data=encode(
                        ACTION_EXPENSE_PAY_CONFIRM, str(expense_id), "",
                        nonce=nonce, ts=ts,
                    ),
                ),
                InlineKeyboardButton(
                    t("expense.pay_cancel", locale),
                    callback_data=encode(ACTION_EXPENSE_PAY, "cancel", str(expense_id)),
                ),
            ]
        )
        kb.append(
            [
                InlineKeyboardButton(
                    t("expense.pay_view_existing", locale),
                    callback_data=encode(ACTION_EXPENSE_DETAIL, str(expense_id)),
                ),
            ]
        )
    else:
        kb.append(
            [
                InlineKeyboardButton(
                    t("expense.pay_now", locale),
                    callback_data=encode(
                        ACTION_EXPENSE_PAY_CONFIRM, str(expense_id), "",
                        nonce=nonce, ts=ts,
                    ),
                ),
                InlineKeyboardButton(
                    t("expense.pay_cancel", locale),
                    callback_data=encode(ACTION_EXPENSE_PAY, "cancel", str(expense_id)),
                ),
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
            )
        ]
    )
    return InlineKeyboardMarkup(kb)


def expense_pay_result_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Payment result card (paid / already paid): back home only."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("common.home", locale), callback_data=encode(ACTION_NAV, "home"))]]
    )


# --- BOT-V1-USABLE-001 P0-2: expense create flow ---------------------------

def expense_confirm_keyboard(
    nonce: str, ts: int, locale: str = "zh"
) -> InlineKeyboardMarkup:
    """Expense confirmation card: [提交审批][修改] + [取消]. ``exc`` creates
    the PENDING expense (Secretary/OWNER both allowed); approval stays the
    Owner-only deterministic path."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("expense.submit_approval", locale),
                    callback_data=encode(
                        ACTION_EXPENSE_CREATE, "exp", nonce=nonce, ts=ts
                    ),
                ),
                InlineKeyboardButton(
                    t("expense.edit", locale),
                    callback_data=encode(ACTION_EXPENSE_EDIT, "menu"),
                ),
            ],
            [
                InlineKeyboardButton(
                    t("rent.cancel", locale), callback_data=encode(ACTION_CANCEL)
                ),
            ],
        ]
    )


def expense_edit_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Edit picker from an expense confirmation card (deterministic states)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("expense.edit_amount", locale),
                    callback_data=encode(ACTION_EXPENSE_EDIT, "amount"),
                ),
                InlineKeyboardButton(
                    t("expense.edit_category", locale),
                    callback_data=encode(ACTION_EXPENSE_EDIT, "cat"),
                ),
            ],
            [
                InlineKeyboardButton(
                    t("expense.edit_unit", locale),
                    callback_data=encode(ACTION_EXPENSE_EDIT, "unit"),
                ),
                InlineKeyboardButton(
                    t("expense.edit_back", locale),
                    callback_data=encode(ACTION_EXPENSE_EDIT, "back"),
                ),
            ],
        ]
    )


def home_summary_keyboard(locale: str = "zh") -> InlineKeyboardMarkup:
    """Home summary actions (P0 spec): view unpaid / approvals / expiring
    contracts / maintenance. These are action buttons, not a second nav."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("home.view_unpaid", locale),
                    callback_data=encode(ACTION_HOME_NAV, "unpaid"),
                ),
                InlineKeyboardButton(
                    t("home.view_approvals", locale),
                    callback_data=encode(ACTION_HOME_NAV, "approvals"),
                ),
            ],
            [
                InlineKeyboardButton(
                    t("home.view_contracts", locale),
                    callback_data=encode(ACTION_HOME_NAV, "contracts"),
                ),
                InlineKeyboardButton(
                    t("home.view_maintenance", locale),
                    callback_data=encode(ACTION_HOME_NAV, "maintenance"),
                ),
            ],
        ]
    )


def ai_choice_keyboard(
    nonce: str, ts: int, options: list[str], locale: str = "zh"
) -> InlineKeyboardMarkup:
    """BOT-V1-USABLE-001 P0-5: AI ambiguity -> 2-3 explicit deterministic
    choices. The callback stores nothing free-form; the tap routes through the
    stored intent payload in the callback handler."""
    kb = [[InlineKeyboardButton(label, callback_data=encode(
        ACTION_AI_CHOICE, "ai", str(idx), nonce=nonce, ts=ts
    ))] for idx, label in enumerate(options[:3])]
    kb.append([InlineKeyboardButton(t("rent.cancel", locale), callback_data=encode(ACTION_CANCEL))])
    return InlineKeyboardMarkup(kb)


def todo_keyboard(
    sections: dict,
    *,
    owner_view: bool,
    locale: str = "zh",
) -> InlineKeyboardMarkup:
    """Unified to-do page action-at-source buttons (V1.3): every row carries
    the action directly — no 'go to the task center' detour."""
    kb: list[list[InlineKeyboardButton]] = []
    for row in sections.get("expenses") or []:
        expense_id = int(row["id"])
        nonce, ts = new_nonce(), now_ts()
        if (row.get("status") or "").lower() == "approved":
            # APPROVED unpaid expense -> the Owner must pay it (PASAY-V2
            # -EXPENSE-PAYABLE-TASK-006 §2/§4). Deterministic pay flow.
            kb.append(
                [
                    InlineKeyboardButton(
                        t("expense.pay_button", locale),
                        callback_data=encode(
                            ACTION_EXPENSE_PAY, str(expense_id), "", nonce=nonce, ts=ts
                        ),
                    ),
                    InlineKeyboardButton(
                        t("expense.view_detail", locale),
                        callback_data=encode(ACTION_EXPENSE_DETAIL, str(expense_id)),
                    ),
                ]
            )
            continue
        kb.append(
            [
                InlineKeyboardButton(
                    _approve_button_label(
                        locale,
                        amount=row.get("amount"),
                        unit=row.get("unit"),
                    ),
                    callback_data=encode(
                        ACTION_EXPENSE_APPROVE, str(expense_id), "", nonce=nonce, ts=ts
                    ),
                ),
                InlineKeyboardButton(
                    _reject_button_label(locale, amount=row.get("amount")),
                    callback_data=encode(
                        ACTION_EXPENSE_REJECT, str(expense_id), "", nonce=nonce, ts=ts
                    ),
                ),
                InlineKeyboardButton(
                    t("expense.view_receipt", locale)
                    if row.get("has_receipt")
                    else t("expense.view_detail", locale),
                    callback_data=encode(ACTION_EXPENSE_DETAIL, str(expense_id)),
                ),
            ]
        )
    for row in sections.get("confirm") or []:
        kb.append(
            [
                InlineKeyboardButton(
                    t("todo.confirm_income", locale),
                    callback_data=encode(
                        ACTION_CONFIRM, "inc", str(row["id"]),
                        nonce=new_nonce(), ts=now_ts(),
                    ),
                )
            ]
        )
    for row in sections.get("overdue") or []:
        kb.append(
            [
                InlineKeyboardButton(
                    f"{t('todo.collect', locale)} · {row.get('unit', '')}",
                    callback_data=encode(ACTION_RENT, "go", str(row["unit_id"])),
                )
            ]
        )
    for task in sections.get("tasks") or []:
        nonce, ts = new_nonce(), now_ts()
        kb.append(
            [
                InlineKeyboardButton(
                    t("ops.complete", locale),
                    callback_data=encode(
                        ACTION_TASK_COMPLETE, "ops", str(task.id), nonce=nonce, ts=ts
                    ),
                ),
                InlineKeyboardButton(
                    t("ops.detail", locale),
                    callback_data=encode(ACTION_TASK_DETAIL, "ops", str(task.id)),
                ),
            ]
        )
    for task in sections.get("maintenance") or []:
        nonce, ts = new_nonce(), now_ts()
        kb.append(
            [
                InlineKeyboardButton(
                    t("ops.complete", locale),
                    callback_data=encode(
                        ACTION_TASK_COMPLETE, "ops", str(task.id), nonce=nonce, ts=ts
                    ),
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
                t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
            )
        ]
    )
    return InlineKeyboardMarkup(kb)


def tasks_quick_keyboard(data, locale: str = "bi") -> InlineKeyboardMarkup:
    """✅ Tasks Quick View action buttons (PASAY-V2-EXPENSE-PAYABLE-TASK-006):
    one deterministic ``Pay`` button per payable APPROVED expense row, then a
    Home button. ``data`` is the backend quick-tasks payload (list of rows,
    or dict with a ``tasks`` key)."""
    tasks = data if isinstance(data, list) else ((data or {}).get("tasks") or [])
    kb: list[list[InlineKeyboardButton]] = []
    for row in tasks:
        if str(row.get("kind") or "") != "payable_expense":
            continue
        expense_id = row.get("expense_id")
        if expense_id is None:
            continue
        nonce, ts = new_nonce(), now_ts()
        kb.append(
            [
                InlineKeyboardButton(
                    f"{t('expense.pay_button', locale)} E{int(expense_id)}",
                    callback_data=encode(
                        ACTION_EXPENSE_PAY, str(int(expense_id)), "", nonce=nonce, ts=ts
                    ),
                )
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
            )
        ]
    )
    return InlineKeyboardMarkup(kb)


# --- TELEGRAM-OPS-UX-CONVERGENCE-001: Quick View action buttons --------------
# All refs are 1-based row indexes resolved by re-fetching the deterministic
# quick-view payload, so no internal unit/expense id ever travels in callback
# data (mirrors the copilot WHY index pattern).


def properties_quick_keyboard(rows, locale: str = "bi") -> InlineKeyboardMarkup:
    """🏠 Properties Quick View inline buttons: one ``👁 1608`` entry per unit
    (opens the unit Quick View) + a ``📄 Property Archive`` deep link. The
    persistent reply keyboard stays pinned client-side."""
    kb: list[list[InlineKeyboardButton]] = []
    for i, row in enumerate(rows, start=1):
        unit = str(row.get("unit_code") or row.get("property_code") or "")
        kb.append(
            [
                InlineKeyboardButton(
                    f"👁 {unit}",
                    callback_data=encode(ACTION_QUICK_UNIT_VIEW, "u", str(i)),
                )
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                t("v2.property_archive", locale),
                callback_data=encode(ACTION_PROP_ARCHIVE),
            )
        ]
    )
    return InlineKeyboardMarkup(kb)


def rent_quick_keyboard(overdue_rows, locale: str = "bi") -> InlineKeyboardMarkup:
    """💰 Rent Quick View overdue actions: one ``1680 Follow up`` button per
    overdue row (opens the Rent detail card), then Home. No repeated generic
    ``Done / Detail`` buttons — each row is a distinct unit."""
    kb: list[list[InlineKeyboardButton]] = []
    for i, row in enumerate(overdue_rows, start=1):
        unit = str(row.get("unit") or row.get("unit_code") or "")
        kb.append(
            [
                InlineKeyboardButton(
                    f"{unit} {t('v2.rent_followup', locale)}",
                    callback_data=encode(ACTION_RENT_QUICK_DETAIL, "ovd", str(i)),
                )
            ]
        )
    kb.append(
        [
            InlineKeyboardButton(
                t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
            )
        ]
    )
    return InlineKeyboardMarkup(kb)


def rent_detail_keyboard(unit_id: int, locale: str = "bi") -> InlineKeyboardMarkup:
    """💰 Rent detail card actions: follow up + record payment + history. The
    record-payment button routes into the existing deterministic rent-collect
    path; follow-up prefers an existing task (dedupe) over a duplicate."""
    kb: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                t("v2.rent_followup", locale),
                callback_data=encode(ACTION_RENT_FOLLOWUP, "r", str(unit_id)),
            ),
            InlineKeyboardButton(
                t("v2.rent_record_payment", locale),
                callback_data=encode(ACTION_RENT, "go", str(unit_id)),
            ),
        ],
        [
            InlineKeyboardButton(
                t("v2.rent_history", locale),
                callback_data=encode(ACTION_DETAIL, "unit", str(unit_id)),
            ),
            InlineKeyboardButton(
                t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
            ),
        ],
    ]
    return InlineKeyboardMarkup(kb)


def expense_remind_keyboard(
    expense_id: int, locale: str = "bi", *, nonce: str = "", ts=None,
) -> InlineKeyboardMarkup:
    """💸 Expense waiting-payment actions: ``🔔 Remind Owner`` (one
    deterministic tap -> one reminder message) + back Home. A fresh nonce per
    rendered card keeps each tap a distinct callback; the handler guards
    rapid repeats so a single gesture never fans out multiple messages."""
    if not nonce:
        nonce = new_nonce()
    if ts is None:
        ts = now_ts()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("v2.remind_owner", locale),
                    callback_data=encode(
                        ACTION_REMIND_OWNER, "exp", str(expense_id), nonce=nonce, ts=ts
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    t("common.home", locale), callback_data=encode(ACTION_NAV, "home")
                )
            ],
        ]
    )
