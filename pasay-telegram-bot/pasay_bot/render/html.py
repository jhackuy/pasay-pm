"""HTML escaping, money formatting, pagination and 4096-UTF-16 truncation.

All user/DB text must pass through :func:`escape` before being embedded in a
message so Telegram never sees raw ``<>&"`` (avoids entity-injection and the
``can't parse entities`` error).
"""
from __future__ import annotations

import html as _html
import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

PESO = "₱"
MAX_MESSAGE_UTF16 = 4096
_TWO_PLACES = Decimal("0.01")


def escape(value) -> str:
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def money(value) -> str:
    """Decimal -> thousands-separated peso string.

    Whole amounts render without decimals (₱55,000); fractional amounts keep
    their two places (₱0.01, ₱1,500,000.50); reversals show a leading minus
    (-₱55,000).

    The value is normalized to two decimal places first so an integer scalar
    (e.g. float 52603.0 or Decimal('52603.0')) never renders as ₱52,603.0 —
    it becomes ₱52,603 (PASAY-V2-EXPENSE-UX-AUDIT-005 §3).
    """
    d = _dec(value)
    sign = "-" if d < 0 else ""
    d = d.quantize(Decimal("0.01"))
    s = format(abs(d), "f")
    int_part, sep, frac = s.partition(".")
    if not int_part:
        int_part = "0"
    int_part = f"{int(int_part):,}"
    if frac == "00":
        return f"{sign}{PESO}{int_part}"
    return f"{sign}{PESO}{int_part}.{frac}"


def percent(part, whole) -> str:
    """Percentage with one decimal; never divides by zero."""
    w = _dec(whole)
    if w == 0:
        return "0.0"
    p = (_dec(part) / w * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return format(p, "f")


def mask_phone(phone: str) -> str:
    """PASAY-AI-EMPLOYEE-FOUNDATION-007 §2/§9: mask a phone for safe echo
    (``0917•••4567``) so a fully-typed number is never broadcast verbatim to a
    group/DM surface."""
    if not phone:
        return ""
    digits = [c for c in phone if c.isdigit()]
    if len(digits) < 4:
        return phone
    head = "".join(digits[:4])
    tail = "".join(digits[-4:])
    prefix = "+" if str(phone).startswith("+") else ""
    return f"{prefix}{head}•••{tail}"


def utf16_len(text) -> int:
    """Telegram counts message length in UTF-16 code units."""
    return len(str(text).encode("utf-16-le")) // 2


_TAG_TOKEN_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")
_TAG_SPLIT_RE = re.compile(r"<[^>]*>")


def _cut_is_safe(s: str) -> bool:
    """True when a truncated prefix ends on a legal boundary: not inside an
    HTML tag or entity, and no unclosed (non-self-closing) tags."""
    if not s:
        return True
    li = s.rfind("<")
    if li != -1 and ">" not in s[li + 1:]:
        return False  # cut inside a tag
    ai = s.rfind("&")
    if ai != -1 and ";" not in s[ai + 1:]:
        return False  # cut inside an entity (e.g. ``&amp``)
    stack: list[str] = []
    for m in _TAG_TOKEN_RE.finditer(s):
        raw = m.group(0)
        name = m.group(1).lower()
        if raw.startswith("</"):
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        elif raw.endswith("/>"):
            continue
        else:
            stack.append(name)
    return not stack


def _utf16_prefix(text: str, budget: int) -> str:
    units = 0
    out: list[str] = []
    for ch in text:
        cu = 2 if ord(ch) > 0xFFFF else 1
        if units + cu > budget:
            break
        units += cu
        out.append(ch)
    return "".join(out)


def truncate(text, limit: int = MAX_MESSAGE_UTF16) -> str:
    """Truncate to at most ``limit`` UTF-16 code units (never splits pairs or
    half-open HTML tags/entities; falls back to pure text when the cut would
    leave an unbalanced tag/entity so Telegram never rejects the message)."""
    text = str(text)
    if utf16_len(text) <= limit:
        return text
    budget = max(limit - 3, 1)
    s = _utf16_prefix(text, budget)
    if not _cut_is_safe(s):
        # Pure-text fallback: strip tags and re-escape so the prefix can never
        # contain a half-open tag. Re-truncation can still land inside a
        # freshly-escaped entity, so trim back until the boundary is legal.
        s = _html.escape(_html.unescape(_TAG_SPLIT_RE.sub("", s)), quote=False)
        s = _utf16_prefix(s, budget)
        while s and not _cut_is_safe(s):
            s = s[:-1]
    return s + "..."


def format_date(value) -> str:
    if value is None:
        return ""
    return str(value)[:10]


def format_month(month: str, locale: str = "zh") -> str:
    year, _, mm = str(month).partition("-")
    if locale == "en":
        names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        if mm.isdigit() and 1 <= int(mm) <= 12:
            return f"{names[int(mm) - 1]} {year}"
        return str(month)
    if mm.isdigit():
        return f"{year}年{int(mm)}月"
    return str(month)


def total_pages(item_count: int, page_size: int) -> int:
    if page_size <= 0:
        return 1
    return max(1, math.ceil(item_count / page_size))


def pagination_footer(page: int, page_size: int, total_items: int, locale: str = "zh") -> str:
    from pasay_bot.render.i18n import t

    total = total_pages(total_items, page_size)
    return t("pagination.footer", locale, page=min(max(page, 1), total), total=total, count=total_items)
