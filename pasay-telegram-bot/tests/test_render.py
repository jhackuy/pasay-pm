"""Renderer tests: escape, money, empty data, pagination, 4096 limit, sorting."""
from datetime import date
from decimal import Decimal

from pasay_bot.api_client import (
    FinancialSummary,
    Income,
    Lease,
    OverdueRent,
    Property,
    Unit,
)
from pasay_bot.render import cards, html as H



def _prop(pid, name="Pasay Premier Residences", address="5 Roxas Blvd"):
    return Property(id=pid, name=name, address=address, city="Pasay", total_units=2)


def _unit(uid, prop_id, number, status="occupied"):
    return Unit(id=uid, property_id=prop_id, unit_number=number, monthly_rent=Decimal("55000"),
                status=status)


def _lease(unit_id=1, rent="55000.00", status="active"):
    return Lease(id=1, unit_id=unit_id, tenant_id=1, start_date=date(2026, 1, 1),
                 end_date=date(2026, 12, 31), monthly_rent=Decimal(rent),
                 status=status)


def _overdue(days, amount, unit="16B", tenant="Juan Dela Cruz", unit_id=1):
    return OverdueRent(lease_id=1, unit_id=unit_id, tenant_id=1, unit=unit, tenant=tenant,
                       overdue_months=1, amount_per_month=Decimal(amount),
                       total_outstanding=Decimal(amount), oldest_due_date=date(2026, 8, 5),
                       overdue_days=days)


def test_property_renderer_chinese():
    text = cards.properties_overview(
        [_prop(1)], {1: {"occupied": 1, "vacant": 1, "total": 2}}, locale="zh"
    )
    assert "🏘 <b>房源概况</b>" in text
    assert "🏢 <b>Pasay Premier Residences</b>" in text
    assert "📍 5 Roxas Blvd" in text
    assert "已出租：1" in text
    assert "空置：1" in text
    assert "出租率：50.0%" in text
    assert "📊 总计：1 套" in text


def test_property_long_address():
    prop = Property(id=1, name="Bayshore", address="5 > 3 Street", city="Pasay", total_units=1)
    text = cards.properties_overview([prop], {1: {"occupied": 0, "vacant": 1, "total": 1}})
    assert "5 &gt; 3 Street" in text
    assert "5 > 3 Street" not in text


def test_property_pagination():
    props = [_prop(i, name=f"Prop {i}") for i in range(1, 13)]
    text = cards.properties_overview(props, {}, page=2, page_size=5)
    assert "第 2/3 页 · 共 12 条" in text
    assert "Prop 1</b>" not in text
    assert "Prop 11</b>" not in text
    assert "Prop 6</b>" in text and "Prop 10</b>" in text


def test_finance_decimal():
    fin = FinancialSummary(
        month="2026-08",
        expected_rent_total=Decimal("363000"),
        collected_rent=Decimal("190000"),
        outstanding_rent=Decimal("173000"),
        total_income=Decimal("721000"),
        total_expense=Decimal("19650"),
        net_income=Decimal("701350"),
        units_count=3, occupied_units=2, vacant_units=1,
    )
    text = cards.finance_card(fin, Decimal("0"))
    assert "💰 <b>2026年8月财务</b>" in text
    assert "应收：₱363,000" in text
    assert "已收：₱190,000" in text
    assert "未收：<b>₱173,000</b>" in text
    assert "收租率：52.3%" in text
    assert "总收入：₱721,000" in text
    assert "总支出：₱19,650" in text
    assert "净收入：<b>₱701,350</b>" in text


def test_finance_overdue_warning():
    fin = FinancialSummary(month="2026-08", expected_rent_total=Decimal("363000"),
                           collected_rent=Decimal("190000"), outstanding_rent=Decimal("173000"),
                           total_income=Decimal("721000"), total_expense=Decimal("19650"),
                           net_income=Decimal("701350"), units_count=3, occupied_units=2, vacant_units=1)
    text = cards.finance_card(fin, Decimal("173000"))
    assert "⚠️ 逾期租金：₱173,000" in text


def test_zero_income():
    fin = FinancialSummary(month="2026-08")
    text = cards.finance_card(fin, Decimal("0"))
    assert "收租率：0.0%" in text
    assert "应收：₱0" in text
    assert "未收：<b>₱0</b>" in text


def test_large_amount():
    assert H.money(Decimal("1500000")) == "₱1,500,000"
    assert H.money(Decimal("1500000.50")) == "₱1,500,000.50"


def test_money_edge_cases():
    assert H.money(Decimal("0")) == "₱0"
    assert H.money(Decimal("0.00")) == "₱0"
    assert H.money(Decimal("0.01")) == "₱0.01"
    assert H.money(Decimal("55000")) == "₱55,000"


def test_reverse_display():
    assert H.money(Decimal("-55000")) == "-₱55,000"
    assert H.money(Decimal("-0.50")) == "-₱0.50"
    assert H.money(Decimal("-1500000")) == "-₱1,500,000"


def test_html_escape():
    prop = Property(id=1, name="Bayshore & Tower", address="5 > 3 Street", city="Pasay", total_units=1)
    unit = _unit(1, 1, "16B")
    lease = _lease()
    text = cards.unit_card(unit, prop.name, prop.address, lease, "Maria <Admin>")
    assert "Bayshore &amp; Tower" in text
    assert "5 &gt; 3 Street" in text
    assert "Maria &lt;Admin&gt;" in text
    assert "&amp;lt;" not in text


def test_empty_tenant():
    unit = _unit(1, 1, "17A", status="vacant")
    text = cards.unit_card(unit, "Prop", "Addr", None, None)
    assert "暂无租客" in text


def test_no_overdue():
    text = cards.overdue_list([], locale="zh")
    assert "暂无逾期租金" in text
    assert "逾期租金 · 0笔" in text


def test_overdue_sort():
    rows = [
        _overdue(days=5, amount="55000", unit="16B", unit_id=1),
        _overdue(days=40, amount="12000", unit="2C", unit_id=2),
        _overdue(days=5, amount="99000", unit="9A", unit_id=3),
    ]
    text = cards.overdue_list(rows, locale="zh")
    assert text.index("2C") < text.index("9A") < text.index("16B")
    assert "逾期：40天" in text


def test_overdue_block_shows_fields():
    row = _overdue(days=5, amount="55000")
    text = cards.overdue_block(row, "Bayshore")
    assert "🔴 <b>Bayshore · Unit 16B</b>" in text
    assert "租客：Juan Dela Cruz" in text
    assert "应付：₱55,000" in text
    assert "到期：2026-08-05" in text
    assert "逾期：5天" in text


def test_empty_properties():
    text = cards.properties_overview([], {})
    assert "暂无房源数据" in text


def test_message_length():
    props = [_prop(i, name=f"Property {i} with a very long name & details") for i in range(1, 60)]
    text = cards.properties_overview(props, {}, page=1, page_size=60)
    truncated = H.truncate(text)
    assert H.utf16_len(truncated) <= 4096
    assert H.utf16_len("正常短文本") <= 4096


def test_truncate_does_not_split_surrogate_pair():
    long_text = "😀" * 5000  # 5000 * 2 UTF-16 units
    truncated = H.truncate(long_text, limit=100)
    assert H.utf16_len(truncated) <= 100
    # No dangling half of a surrogate pair.
    assert not truncated or truncated.endswith("...")


def test_pagination():
    assert H.total_pages(12, 5) == 3
    assert H.total_pages(0, 5) == 1
    assert H.total_pages(5, 5) == 1
    assert H.pagination_footer(1, 5, 12, "zh") == "第 1/3 页 · 共 12 条"


def test_finance_title_en():
    fin = FinancialSummary(month="2026-08")
    text = cards.finance_card(fin, Decimal("0"), locale="en")
    assert "Aug 2026 Finance" in text


def test_income_status_emoji_consistency():
    assert cards.overdue_emoji(5) == "🔴"
    assert cards.unit_status_label("occupied") == "🟢 已出租"
    assert cards.unit_status_label("vacant") == "⚪ 空置"
    assert cards.unit_status_label("weird") == "🔵 待处理"


# --- F8: truncation never leaves a half-open tag/entity ---

def test_truncate_never_splits_entity_or_tag():
    long_text = "<b>" + "Bayshore &amp; Tower " * 300 + "</b>"
    truncated = H.truncate(long_text, limit=100)
    assert H.utf16_len(truncated) <= 100
    ai = truncated.rfind("&")
    if ai != -1:
        assert ";" in truncated[ai + 1:], truncated
    if "<" in truncated:
        assert truncated.count("<b>") == truncated.count("</b>")
    else:
        assert "&amp;" in truncated or "&lt;" in truncated or "&gt;" in truncated


def test_truncate_long_list_with_ampersand_names():
    props = [
        Property(id=i, name=f"Bayshore & Tower {i}", address="5 > 3 Street",
                 city="Pasay", total_units=1)
        for i in range(1, 60)
    ]
    text = cards.properties_overview(props, {}, page=1, page_size=60)
    truncated = H.truncate(text)
    assert H.utf16_len(truncated) <= 4096
    ai = truncated.rfind("&")
    if ai != -1:
        assert ";" in truncated[ai + 1:], truncated
    if "<" in truncated:
        assert truncated.count("<b>") == truncated.count("</b>")
