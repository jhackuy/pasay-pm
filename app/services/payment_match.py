"""Entry B rent-payment matching service (V1.3 Slice 2, partial payments).

Read-only resolution of an owner's natural-language payment statement
("1608租金收到了", "John的70000到了", "昨天收到1608房租") to the most likely
open rent receivable, with a confidence grade. This service NEVER writes a
financial record: confirmation keeps flowing through the existing Income
create + Owner-only confirm chain, preserving DB-level idempotency, atomic
writes, audit rows and evidence relations.

SLICE2-RENT-005: a statement can carry a partial amount ("1608 付了 40000")
or a follow-up ("1608 又付了 30000"). The matcher reports the matched period
receivable (``due_amount``), the confirmed amount before this payment
(``paid_amount``) and the balance remaining AFTER this payment
(``remaining_balance``). Payments that would push the cumulative amount over
the period receivable are never suggested for confirmation — they surface as
an OVERPAYMENT candidate so the bot can explain without writing anything.
Overpayment / prepayment / next-month offset stays a future slice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.financial import Income, IncomeStatus
from app.models.lease import Lease, LeaseStatus
from app.models.property import Property, Unit
from app.models.tenant import Tenant
from app.services.operations.rent_math import (
    confirmed_paid_by_period,
    lease_periods,
    month_from_description,
    period_remaining,
)

TWO_PLACES = Decimal("0.01")
MIN_AMOUNT_DIGITS = 1000  # below this a bare number is treated as a unit hint, not money

_CURRENCY_NUMBER = re.compile(
    r"(?:₱|PHP|Php|php|比索|peso|pesos|(?<![A-Za-z])[Pp])"
    r"\s*(\d[\d,]*(?:\.\d+)?)"
)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TOKEN = re.compile(r"[A-Za-z0-9._-]+")
_ZERO_AMOUNT = re.compile(r"(?<!\d)0(?:\.0+)?(?!\d)")
_NEGATIVE_AMOUNT = re.compile(r"(?<!\d)[-−]\s*\d[\d,]*")
_DATE_KEYWORDS = {
    "大前天": -3,
    "前天": -2,
    "昨天": -1,
    "今天": 0,
    "今日": 0,
}
_FULL_DATE = re.compile(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_CN_DATE = re.compile(r"(?<![\d.])(\d{1,2})月(\d{1,2})日")
_PERIOD_HINT = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(?:租金|房租|租|份)?")


class MatchConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MatchKind(str, Enum):
    OPEN = "open"            # open receivable -> show the confirm card
    PENDING = "pending"      # a matching pending income already exists -> confirm that one
    DUPLICATE = "duplicate"  # already booked -> friendly "already recorded" message
    OVERPAYMENT = "overpayment"  # amount would exceed the period receivable -> explain, never book
    INVALID_AMOUNT = "invalid_amount"  # zero/negative amount -> friendly error, never book


@dataclass
class ParsedHints:
    unit_hints: list[str] = field(default_factory=list)
    amounts: list[Decimal] = field(default_factory=list)
    received_date: date = field(default_factory=date.today)
    period_hint: Optional[str] = None
    invalid_amount: bool = False


@dataclass
class LeaseCtx:
    """One active lease plus the surrounding rows the matcher needs."""

    lease: Lease
    unit: Unit
    property: Property
    tenant: Tenant
    periods: list[tuple[str, date]] = field(default_factory=list)
    paid_by_period: dict[str, Decimal] = field(default_factory=dict)
    pending_periods: set[str] = field(default_factory=set)
    incomes: list[Income] = field(default_factory=list)

    @property
    def open_periods(self) -> list[tuple[str, date]]:
        """Periods whose confirmed total is still below the monthly rent
        (amount-aware: a partial payment keeps the period open until it is
        fully settled)."""
        rent = self.lease.monthly_rent.quantize(TWO_PLACES)
        return [
            (m, d)
            for m, d in self.periods
            if period_remaining(
                rent, self.paid_by_period.get(m, Decimal("0.00"))
            ) > 0
            and m not in self.pending_periods
        ]


@dataclass
class MatchCandidate:
    kind: MatchKind
    confidence: MatchConfidence
    lease_id: int
    unit_id: int
    unit_number: str
    property_id: int
    property_name: str
    tenant_id: int
    tenant_name: str
    period: str
    due_date: Optional[date]
    amount: Decimal
    open_count: int = 0
    due_amount: Decimal = Decimal("0.00")
    paid_amount: Decimal = Decimal("0.00")
    remaining_balance: Decimal = Decimal("0.00")
    income_id: Optional[int] = None
    income_status: Optional[str] = None


@dataclass
class MatchResult:
    received_date: date
    candidates: list[MatchCandidate] = field(default_factory=list)

    @property
    def best(self) -> Optional[MatchCandidate]:
        return self.candidates[0] if self.candidates else None


# ---------------------------------------------------------------------------
# text parsing (pure, unit-testable)
# ---------------------------------------------------------------------------


def _unit_matches(token: str, unit_number: str) -> bool:
    """Normalized unit-number match.

    Trailing sentence punctuation ("1608.") must not hide the unit hint.
    Suffix matches require a non-digit boundary so a short token like "608"
    never over-matches unit "1608" / "DEV-BAY-1608" (and "20" never matches
    "1020"), while "1608" still resolves the prefixed building-unit style
    ("DEV-BAY-1608") used by the dev data.
    """
    t = (token or "").lower().strip().rstrip(".,;:!?")
    u = (unit_number or "").lower().strip()
    if not u or not t:
        return False
    if t == u:
        return True
    if u.endswith(t) and not u[len(u) - len(t) - 1].isdigit():
        return True
    if t.endswith(u) and not t[len(t) - len(u) - 1].isdigit():
        return True
    return False


def _parse_unit_hints(text: str, unit_numbers: Iterable[str]) -> list[str]:
    units = sorted({u for u in unit_numbers if u})
    hints: list[str] = []
    for token in _TOKEN.findall(text):
        # Normalize the hint (strip sentence punctuation) so downstream
        # consumers see the unit reference, not the raw token.
        normalized = (token or "").lower().strip().rstrip(".,;:!?")
        if normalized and any(_unit_matches(token, u) for u in units) and normalized not in hints:
            hints.append(normalized)
    return hints


def _parse_amounts(text: str, unit_numbers: Iterable[str]) -> list[Decimal]:
    units = sorted({u for u in unit_numbers if u})
    amounts: list[Decimal] = []
    for match in _CURRENCY_NUMBER.finditer(text):
        try:
            amounts.append(Decimal(match.group(1).replace(",", "")))
        except Exception:
            continue
    if not amounts:
        for match in _NUMBER.finditer(text):
            token = match.group(0)
            if any(_unit_matches(token, u) for u in units):
                continue
            try:
                value = Decimal(token.replace(",", ""))
            except Exception:
                continue
            if value >= MIN_AMOUNT_DIGITS:
                amounts.append(value)
    return amounts


def _has_invalid_amount(text: str) -> bool:
    """Zero / negative amounts are never bookable. Date separators
    ("2026-08-10") are excluded by the non-digit lookbehind on the minus."""
    if _NEGATIVE_AMOUNT.search(text):
        return True
    return bool(_ZERO_AMOUNT.search(text))


def _parse_date_hint(text: str, today: date) -> date:
    for keyword, offset in _DATE_KEYWORDS.items():
        if keyword in text:
            return today + timedelta(days=offset)
    for pattern in (_FULL_DATE, _ISO_DATE):
        match = pattern.search(text)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                break
    match = _CN_DATE.search(text)
    if match:
        try:
            return date(today.year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            return today
    return today


def _parse_period_hint(text: str, today: date) -> Optional[str]:
    if _FULL_DATE.search(text) or _ISO_DATE.search(text) or _CN_DATE.search(text):
        return None  # a concrete date wins over a bare "8月" period hint
    match = _PERIOD_HINT.search(text)
    if not match:
        return None
    year = int(match.group(1)) if match.group(1) else today.year
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{year:04d}-{month:02d}"


def parse_hints(text: str, unit_numbers: Iterable[str], today: Optional[date] = None) -> ParsedHints:
    """Extract deterministic hints from a rent-payment statement."""
    today = today or date.today()
    units = sorted({u for u in unit_numbers if u})
    return ParsedHints(
        unit_hints=_parse_unit_hints(text, units),
        amounts=_parse_amounts(text, units),
        received_date=_parse_date_hint(text, today),
        period_hint=_parse_period_hint(text, today),
        invalid_amount=_has_invalid_amount(text),
    )


def _tenant_hints(text: str, ctxs: list[LeaseCtx]) -> list[str]:
    lowered = text.lower()
    hints: list[str] = []
    for ctx in ctxs:
        name = (ctx.tenant.full_name or "").strip()
        if not name:
            continue
        name_lower = name.lower()
        if name_lower in lowered:
            if name not in hints:
                hints.append(name)
            continue
        for token in _TOKEN.findall(name_lower):
            if len(token) >= 3 and token in lowered and token not in hints:
                hints.append(token)
    return hints


def _income_period(ctx: LeaseCtx, income: Income) -> Optional[str]:
    month = month_from_description(income.description)
    if month is None:
        month = income.received_date.strftime("%Y-%m")
    if month in {m for m, _ in ctx.periods}:
        return month
    return None


def _resolve_amount(amounts: list[Decimal], rows: list, explicit_amount: Optional[Decimal]) -> Optional[Decimal]:
    if explicit_amount is not None:
        return explicit_amount.quantize(TWO_PLACES)
    if not amounts:
        return None
    distinct = {a.quantize(TWO_PLACES) for a in amounts}
    row_amounts = {row.ctx.lease.monthly_rent.quantize(TWO_PLACES) for row in rows}
    hits = [a for a in distinct if a in row_amounts]
    if len(hits) == 1:
        return hits[0]
    return max(distinct)


def _remaining_for(ctx: LeaseCtx, period: str) -> Decimal:
    """Confirmed total still owed for one period (never negative)."""
    rent = ctx.lease.monthly_rent.quantize(TWO_PLACES)
    paid = ctx.paid_by_period.get(period, Decimal("0.00")).quantize(TWO_PLACES)
    return period_remaining(rent, paid).quantize(TWO_PLACES)


def _ctx_matches_hints(ctx: LeaseCtx, hints: ParsedHints) -> bool:
    if hints.unit_hints:
        if not any(_unit_matches(h, ctx.unit.unit_number) for h in hints.unit_hints):
            return False
    if hints.tenant_hints:
        name = (ctx.tenant.full_name or "").lower()
        if not any(h.lower() in name for h in hints.tenant_hints):
            return False
    return True


def _build_open_candidate(row, rows: list, hints: ParsedHints, resolved_amount: Optional[Decimal]) -> MatchCandidate:
    ctx = row.ctx
    open_count = sum(1 for r in rows if r.ctx is ctx)
    if len(rows) == 1 and open_count == 1:
        confidence = MatchConfidence.HIGH
    elif len(rows) == 1:
        confidence = MatchConfidence.MEDIUM
    else:
        confidence = MatchConfidence.LOW
    rent = ctx.lease.monthly_rent.quantize(TWO_PLACES)
    paid = ctx.paid_by_period.get(row.period, Decimal("0.00")).quantize(TWO_PLACES)
    remaining_before = _remaining_for(ctx, row.period)
    # A statement without an explicit amount is interpreted as settling the
    # whole outstanding balance of the matched period (full rent when the
    # period is still untouched, the remaining balance once partially paid).
    amount = (
        resolved_amount.quantize(TWO_PLACES)
        if resolved_amount is not None
        else remaining_before
    )
    remaining_after = (
        period_remaining(remaining_before, amount)
        + (open_count - 1) * rent
    ).quantize(TWO_PLACES)
    return MatchCandidate(
        kind=MatchKind.OPEN,
        confidence=confidence,
        lease_id=ctx.lease.id,
        unit_id=ctx.unit.id,
        unit_number=ctx.unit.unit_number,
        property_id=ctx.property.id,
        property_name=ctx.property.name,
        tenant_id=ctx.tenant.id,
        tenant_name=ctx.tenant.full_name,
        period=row.period,
        due_date=row.due,
        amount=amount,
        open_count=open_count,
        due_amount=rent,
        paid_amount=paid,
        remaining_balance=remaining_after,
    )


def _build_overpayment_candidate(row, resolved_amount: Decimal) -> MatchCandidate:
    """A payment that would exceed the matched period's remaining receivable.
    Never suggests confirmation — the bot only explains and writes nothing."""
    ctx = row.ctx
    rent = ctx.lease.monthly_rent.quantize(TWO_PLACES)
    paid = ctx.paid_by_period.get(row.period, Decimal("0.00")).quantize(TWO_PLACES)
    return MatchCandidate(
        kind=MatchKind.OVERPAYMENT,
        confidence=MatchConfidence.HIGH,
        lease_id=ctx.lease.id,
        unit_id=ctx.unit.id,
        unit_number=ctx.unit.unit_number,
        property_id=ctx.property.id,
        property_name=ctx.property.name,
        tenant_id=ctx.tenant.id,
        tenant_name=ctx.tenant.full_name,
        period=row.period,
        due_date=row.due,
        amount=resolved_amount.quantize(TWO_PLACES),
        open_count=0,
        due_amount=rent,
        paid_amount=paid,
        remaining_balance=_remaining_for(ctx, row.period),
    )


def _build_invalid_candidate() -> MatchCandidate:
    """Zero / negative amount: friendly error candidate, nothing is written."""
    return MatchCandidate(
        kind=MatchKind.INVALID_AMOUNT,
        confidence=MatchConfidence.HIGH,
        lease_id=0,
        unit_id=0,
        unit_number="",
        property_id=0,
        property_name="",
        tenant_id=0,
        tenant_name="",
        period="",
        due_date=None,
        amount=Decimal("0.00"),
        open_count=0,
        due_amount=Decimal("0.00"),
        paid_amount=Decimal("0.00"),
        remaining_balance=Decimal("0.00"),
    )


def _match_existing(ctxs: list[LeaseCtx], hints: ParsedHints, resolved_amount: Optional[Decimal]) -> list[MatchCandidate]:
    """No open receivable remains: surface an existing pending/confirmed income
    so the bot can confirm it (never create a second record) or tell the user
    the payment was already booked. With partial amounts, an identical pending
    payment is always surfaced (confirm THAT row); an identical CONFIRMED
    partial is only a duplicate once the period is fully covered — otherwise
    the OPEN path still allows another genuine payment of the same amount."""
    if not (hints.unit_hints or hints.tenant_hints or hints.period_hint or resolved_amount is not None):
        return []
    candidates: list[MatchCandidate] = []
    for ctx in ctxs:
        if not _ctx_matches_hints(ctx, hints):
            continue
        rent = ctx.lease.monthly_rent.quantize(TWO_PLACES)
        for income in ctx.incomes:
            if income.status not in (IncomeStatus.pending, IncomeStatus.confirmed):
                continue
            period = _income_period(ctx, income)
            if period is None:
                continue
            if hints.period_hint and period != hints.period_hint:
                continue
            inc_amount = income.amount.quantize(TWO_PLACES)
            if resolved_amount is not None:
                if inc_amount != resolved_amount:
                    continue
                if income.status == IncomeStatus.pending:
                    kind = MatchKind.PENDING
                elif inc_amount >= rent or _remaining_for(ctx, period) <= 0:
                    # Full amount, or a partial after the period is settled:
                    # re-reporting it means "already booked", not a new payment.
                    kind = MatchKind.DUPLICATE
                else:
                    continue  # OPEN path may accept another partial of this size
            else:
                if inc_amount != rent:
                    continue
                kind = (
                    MatchKind.PENDING
                    if income.status == IncomeStatus.pending
                    else MatchKind.DUPLICATE
                )
            paid = ctx.paid_by_period.get(period, Decimal("0.00")).quantize(TWO_PLACES)
            balance_after = _remaining_for(ctx, period)
            if kind == MatchKind.PENDING:
                balance_after = period_remaining(balance_after, inc_amount).quantize(TWO_PLACES)
            candidates.append(
                MatchCandidate(
                    kind=kind,
                    confidence=MatchConfidence.HIGH,
                    lease_id=ctx.lease.id,
                    unit_id=ctx.unit.id,
                    unit_number=ctx.unit.unit_number,
                    property_id=ctx.property.id,
                    property_name=ctx.property.name,
                    tenant_id=ctx.tenant.id,
                    tenant_name=ctx.tenant.full_name,
                    period=period,
                    due_date=dict(ctx.periods).get(period),
                    amount=inc_amount,
                    open_count=0,
                    due_amount=rent,
                    paid_amount=paid,
                    remaining_balance=balance_after,
                    income_id=income.id,
                    income_status=income.status.value,
                )
            )
    if not candidates:
        return []
    # de-duplicate by (kind, income_id); confidence drops when ambiguous.
    seen: set[tuple[str, int]] = set()
    unique: list[MatchCandidate] = []
    for cand in candidates:
        key = (cand.kind.value, cand.income_id or 0)
        if key in seen:
            continue
        seen.add(key)
        unique.append(cand)
    if len(unique) > 1:
        for cand in unique:
            cand.confidence = MatchConfidence.MEDIUM
    return unique


def _rank(candidates: list[MatchCandidate]) -> list[MatchCandidate]:
    rank = {
        MatchKind.PENDING: 0,
        MatchKind.DUPLICATE: 1,
        MatchKind.OPEN: 2,
    }
    conf_rank = {
        MatchConfidence.HIGH: 0,
        MatchConfidence.MEDIUM: 1,
        MatchConfidence.LOW: 2,
    }
    return sorted(
        candidates,
        key=lambda c: (
            conf_rank[c.confidence],
            rank[c.kind],
            -(c.income_id or 0),
            c.period,
        ),
    )


def match_from_leases(
    ctxs: list[LeaseCtx],
    text: str,
    today: Optional[date] = None,
    amount: Optional[Decimal] = None,
) -> MatchResult:
    """Core matcher: pure over the loaded lease contexts (no DB access)."""
    today = today or date.today()
    hints = parse_hints(text, [ctx.unit.unit_number for ctx in ctxs], today)
    hints.tenant_hints = _tenant_hints(text, ctxs)
    if hints.invalid_amount:
        return MatchResult(
            received_date=hints.received_date,
            candidates=[_build_invalid_candidate()],
        )

    # "Open receivable" = unpaid periods up to the CURRENT month. Future
    # months are not billed yet and must not inflate the unique-outstanding
    # count (a tenant who pays on time has exactly one open bill).
    cutoff = f"{today:%Y-%m}"
    rows = [
        (ctx, period, due)
        for ctx in ctxs
        for period, due in ctx.open_periods
        if period <= cutoff
    ]
    if hints.unit_hints:
        rows = [r for r in rows if any(_unit_matches(h, r[0].unit.unit_number) for h in hints.unit_hints)]
    if hints.tenant_hints:
        rows = [
            r for r in rows
            if any(h.lower() in (r[0].tenant.full_name or "").lower() for h in hints.tenant_hints)
        ]
    resolved_amount = _resolve_amount(hints.amounts, [_Row(r) for r in rows], amount)
    if hints.period_hint:
        rows = [r for r in rows if r[1] == hints.period_hint]

    if resolved_amount is not None:
        affordable = [
            r for r in rows
            if _remaining_for(r[0], r[1]) >= resolved_amount.quantize(TWO_PLACES)
        ]
        if rows and not affordable:
            # Every matched open bill would be overpaid by this amount:
            # explain and stop (no write, no prepayment in this slice).
            return MatchResult(
                received_date=hints.received_date,
                candidates=[_build_overpayment_candidate(_Row(rows[0]), resolved_amount)],
            )
        rows = affordable

    if rows:
        candidates = [_build_open_candidate(_Row(r), [_Row(x) for x in rows], hints, resolved_amount) for r in rows]
        return MatchResult(received_date=hints.received_date, candidates=_rank(candidates))

    # No open receivable remains for the matched lease(s): an identical
    # pending/confirmed record is surfaced (confirm that row / "already
    # booked") instead of ever booking a second record.
    existing = _match_existing(ctxs, hints, resolved_amount)
    return MatchResult(received_date=hints.received_date, candidates=_rank(existing))


class _Row:
    """Tiny adapter so open-row helpers share one shape."""

    def __init__(self, triple):
        self.ctx, self.period, self.due = triple


def load_contexts(db: Session) -> list[LeaseCtx]:
    """Load active leases + related rows + their incomes (read-only)."""
    leases = (
        db.query(Lease)
        .filter(Lease.status == LeaseStatus.active, Lease.deleted_at.is_(None))
        .all()
    )
    if not leases:
        return []
    lease_ids = [lease.id for lease in leases]
    unit_ids = {l.unit_id for l in leases}
    tenant_ids = {l.tenant_id for l in leases}
    property_ids = {u.property_id for u in db.query(Unit).filter(Unit.id.in_(unit_ids)).all()}
    units = {u.id: u for u in db.query(Unit).filter(Unit.id.in_(unit_ids)).all()}
    tenants = {t.id: t for t in db.query(Tenant).filter(Tenant.id.in_(tenant_ids)).all()}
    properties = {p.id: p for p in db.query(Property).filter(Property.id.in_(property_ids)).all()}
    incomes_by_lease: dict[int, list[Income]] = {}
    for income in (
        db.query(Income)
        .filter(Income.lease_id.in_(lease_ids))
        .all()
    ):
        if income.lease_id is not None:
            incomes_by_lease.setdefault(income.lease_id, []).append(income)

    ctxs: list[LeaseCtx] = []
    for lease in leases:
        unit = units.get(lease.unit_id)
        tenant = tenants.get(lease.tenant_id)
        if unit is None or tenant is None:
            continue
        prop = properties.get(unit.property_id)
        if prop is None:
            continue
        periods = lease_periods(lease)
        incomes = incomes_by_lease.get(lease.id, [])
        confirmed = [i for i in incomes if i.status == IncomeStatus.confirmed]
        pending = [i for i in incomes if i.status == IncomeStatus.pending]
        ctx = LeaseCtx(
            lease=lease,
            unit=unit,
            property=prop,
            tenant=tenant,
            periods=periods,
            paid_by_period=confirmed_paid_by_period(periods, confirmed),
            incomes=incomes,
        )
        for income in pending:
            period = month_from_description(income.description)
            if period is None:
                period = income.received_date.strftime("%Y-%m")
            if period in {m for m, _ in periods}:
                ctx.pending_periods.add(period)
        ctxs.append(ctx)
    return ctxs


def match_payment(
    db: Session,
    text: str,
    amount: Optional[Decimal] = None,
    today: Optional[date] = None,
) -> MatchResult:
    """Public entry: load contexts and match (read-only, never writes)."""
    return match_from_leases(load_contexts(db), text, today=today, amount=amount)
