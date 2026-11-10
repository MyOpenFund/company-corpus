from collections import Counter
from decimal import Decimal

import pytest

from company_corpus.config import DEFAULT_CURRENCY_ANCHOR_TAGS, DEFAULT_DURATION_BANDS
from company_corpus.opendata.fundamentals import (
    classify_duration,
    flatten_with_namespace,
    as_filed_points,
)
from tests.opendata.factories import companyfacts, point


def _select(us_gaap, dei=None):
    counters = Counter()
    pts = as_filed_points(flatten_with_namespace(companyfacts(us_gaap, dei), ("us-gaap", "dei")),
                        DEFAULT_DURATION_BANDS, counters, DEFAULT_CURRENCY_ANCHOR_TAGS)
    return pts, counters


@pytest.mark.parametrize("days, expected", [
    (91, "quarterly"), (112, "quarterly"),        # 13 and 16 weeks (Costco Q4)
    (80, None), (116, None),
    (182, "semiannual"), (196, "semiannual"),     # 26 and 28 weeks
    (273, "ytd9"), (364, "annual"), (371, "annual"), (366, "annual"),
    (336, None), (395, None), (31, None),         # 10-KT transition, odd, monthly
])
def test_fiscal_week_bands(days, expected):
    assert classify_duration(days, DEFAULT_DURATION_BANDS) == expected


def test_instant_and_durations_get_their_frequency():
    pts, c = _select({
        "LongTermDebt": {"USD": [point(100, "2024-06-30")]},
        "Revenues": {"USD": [
            point(10, "2024-06-30", start="2024-04-01", form="10-Q"),       # 91 days
            point(20, "2024-06-30", start="2024-01-01", form="10-Q"),       # 182 days: 6m YTD
            point(30, "2024-09-30", start="2024-01-01", form="10-Q"),       # 274 days: 9m YTD
            point(40, "2024-06-30", start="2023-07-01"),                    # 366 days
            point(50, "2024-06-30", start="2023-08-01", form="10-KT"),      # 335 days
        ]},
    })
    got = sorted((p.tag, p.frequency, p.value) for p in pts)
    assert got == [
        ("LongTermDebt", "instant", Decimal(100)),
        ("Revenues", "annual", Decimal(40)),
        ("Revenues", "quarterly", Decimal(10)),
        ("Revenues", "semiannual", Decimal(20)),
        ("Revenues", "ytd9", Decimal(30)),
    ]
    assert c["dropped:ytd9"] == 0 and c["dropped:odd_duration"] == 1


def test_every_dictionary_tag_is_kept_whatever_its_place_in_the_chain():
    pts, _ = _select({
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": [
            point(100, "2024-06-30", start="2023-07-01")]},
        "Revenues": {"USD": [
            point(101, "2024-06-30", start="2023-07-01"),                       # same filing
            point(90, "2023-06-30", start="2022-07-01", accn="0000000042-23-000001")]},
    })
    got = sorted((p.end.isoformat(), p.tag, p.value) for p in pts)
    assert got == [
        ("2023-06-30", "Revenues", Decimal(90)),
        ("2024-06-30", "RevenueFromContractWithCustomerExcludingAssessedTax", Decimal(100)),
        ("2024-06-30", "Revenues", Decimal(101)),
    ]


def test_a_proxy_statement_is_not_a_financial_report():
    pts, c = _select({"NetIncomeLoss": {"USD": [
        point(590185000, "2025-12-31", start="2025-01-01"),
        point(609467, "2025-12-31", start="2025-01-01", accn="0001104659-26-045753",
              form="DEF 14A", filed="2026-04-21")]}})
    assert [p.value for p in pts] == [Decimal(590185000)] and c["dropped:not_a_report"] == 1


def test_a_helper_tag_is_ingested_and_a_tag_out_of_the_dictionary_is_not():
    pts, _ = _select({
        "LiabilitiesAndStockholdersEquity": {"USD": [point(5, "2024-06-30")]},
        "SomeOtherTag": {"USD": [point(6, "2024-06-30")]},
    })
    assert [p.tag for p in pts] == ["LiabilitiesAndStockholdersEquity"]


def test_convenience_translation_and_unexpected_units_are_dropped():
    pts, c = _select({
        "Revenues": {
            "USD": [point(10, "2024-06-30", start="2023-07-01"),
                    point(11, "2023-06-30", start="2022-07-01"),
                    point(12, "2022-06-30", start="2021-07-01")],
            "EUR": [point(9, "2024-06-30", start="2023-07-01")]},
        "EarningsPerShareDiluted": {"EUR/shares": [point(1.5, "2024-06-30", start="2023-07-01")],
                                     "USD/shares": [point(1.6, "2024-06-30", start="2023-07-01")]},
        "LongTermDebt": {"shares": [point(3, "2024-06-30")]},
    })
    assert sorted((p.tag, p.unit) for p in pts) == [
        ("EarningsPerShareDiluted", "USD/shares"), ("Revenues", "USD"), ("Revenues", "USD"),
        ("Revenues", "USD")]
    assert c["dropped:non_reporting_currency"] == 2 and c["dropped:unexpected_unit"] == 1


def test_dei_fallback_tag_keeps_its_namespace():
    pts, _ = _select({}, dei={"EntityCommonStockSharesOutstanding": {
        "shares": [point(7426194552, "2026-10-22", form="10-Q")]}})
    (p,) = pts
    assert (p.ns, p.tag, p.frequency) == ("dei", "EntityCommonStockSharesOutstanding", "instant")


def test_bad_points_and_period_shapes_are_counted():
    pts, c = _select({
        "Revenues": {"USD": [point(None, "2024-06-30", start="2023-07-01"),
                             point(5, "2024-13-01", start="2023-07-01"),
                             point(6, "2024-06-30", start="2023-07-01", accn=""),
                             point(7, "2024-06-30")]},                 # revenue as an instant
        "LongTermDebt": {"USD": [point(8, "2024-06-30", start="2023-07-01")]},  # debt as a flow
    })
    assert pts == []
    assert c["dropped:bad_point"] == 3 and c["dropped:period_shape"] == 2


def _rev(value, end, accn):
    return point(value, end, start=f"{int(end[:4]) - 1}{end[4:]}", accn=accn)


def test_the_reporting_currency_is_that_of_each_filing():
    # An issuer switching currency: the later, more numerous filings must not change which
    # points of the earlier one are selected, or a published row would change (conflict).
    early = {"Revenues": {"USD": [_rev(100, "2020-12-31", "A1")]}}
    late = {"Revenues": {"USD": [_rev(100, "2020-12-31", "A1")],
                         "EUR": [_rev(90, f"{y}-12-31", f"B{y}") for y in (2021, 2022, 2023)]}}
    before, _ = _select(early)
    after, c = _select(late)
    assert [(p.accn, p.value, p.unit) for p in before] == [("A1", Decimal(100), "USD")]
    assert ("A1", Decimal(100), "USD") in [(p.accn, p.value, p.unit) for p in after]
    assert len(after) == 4 and c["dropped:non_reporting_currency"] == 0


def test_a_convenience_translation_inside_a_filing_is_still_dropped():
    pts, c = _select({"Revenues": {
        "EUR": [_rev(90, "2023-12-31", "A1"), _rev(80, "2022-12-31", "A1")],
        "USD": [_rev(99, "2023-12-31", "A1")]}})
    assert {p.unit for p in pts} == {"EUR"} and c["dropped:non_reporting_currency"] == 1


def test_a_dei_value_in_another_currency_does_not_vote():
    # One curated EUR fact; the cover page reports the public float in USD. Only us-gaap
    # facts vote, so the filing is EUR and its point is kept.
    pts, c = _select({"Revenues": {"EUR": [_rev(90, "2023-12-31", "A1")]}},
                     dei={"EntityPublicFloat": {"USD": [point(5, "2023-06-30", accn="A1")]}})
    assert [(p.value, p.unit) for p in pts] == [(Decimal(90), "EUR")]
    assert c["dropped:non_reporting_currency"] == 0


@pytest.mark.parametrize("order", [("EUR", "GBP"), ("GBP", "EUR"), ("EUR", "USD"), ("USD", "EUR")])
def test_a_currency_tie_is_never_guessed_whatever_the_tag_order(order):
    us_gaap = {tag: {unit: [_rev(i + 1, "2023-12-31", "A1")]}
               for i, (tag, unit) in enumerate(zip(("Revenues", "NetIncomeLoss"), order))}
    pts, c = _select(us_gaap)
    assert pts == [] and c["dropped:currency_ambiguous"] == 2


# --- The reporting currency: shapes taken from real filings, and the fail-safe.


def _inst(value, end, accn="A1"):
    return point(value, end, accn=accn)


def test_convenience_column_first_on_the_balance_sheet_keeps_the_real_currency():
    # Baidu's 2013 20-F: USD convenience for the latest year, CNY for both years.
    pts, c = _select({"Assets": {"USD": [_inst(11_726_016, "2013-12-31")],
                                 "CNY": [_inst(70_985_788, "2013-12-31"),
                                         _inst(45_668_890, "2012-12-31")]}})
    assert {p.unit for p in pts} == {"CNY"} and len(pts) == 2
    assert c["dropped:non_reporting_currency"] == 1 and c["dropped:currency_ambiguous"] == 0


def test_a_balance_sheet_that_contradicts_the_vote_publishes_no_money():
    # Most facts in USD, but the balance sheet totals in EUR: one signal must be wrong.
    pts, c = _select({
        "Assets": {"EUR": [_inst(100, "2024-06-30"), _inst(90, "2023-06-30")]},
        "Revenues": {"USD": [_rev(1, "2024-06-30", "A1"), _rev(2, "2023-06-30", "A1"),
                             _rev(3, "2022-06-30", "A1")]},
    })
    assert pts == [] and c["dropped:currency_ambiguous"] == 5


def test_a_tied_vote_is_settled_by_the_balance_sheet():
    pts, c = _select({
        "Assets": {"EUR": [_inst(100, "2024-06-30")]},
        "Revenues": {"USD": [_rev(1, "2024-06-30", "A1"), _rev(2, "2023-06-30", "A1")],
                     "EUR": [_rev(3, "2022-06-30", "A1")]},
    })
    # USD 2 vs EUR 2, balance sheet in EUR.
    assert {p.unit for p in pts} == {"EUR"} and c["dropped:currency_ambiguous"] == 0


def test_a_tie_with_no_balance_sheet_publishes_no_money():
    pts, c = _select({"Revenues": {"USD": [_rev(1, "2024-06-30", "A1")],
                                   "EUR": [_rev(2, "2023-06-30", "A1")]}})
    assert pts == [] and c["dropped:currency_ambiguous"] == 2


def test_a_tied_balance_sheet_leaves_the_vote_in_charge():
    pts, c = _select({
        "Assets": {"USD": [_inst(100, "2024-06-30")], "EUR": [_inst(90, "2024-06-30")]},
        "Revenues": {"EUR": [_rev(3, "2024-06-30", "A1")]},
    })
    assert {p.unit for p in pts} == {"EUR"} and c["dropped:currency_ambiguous"] == 0


def test_an_ambiguous_filing_still_gives_its_share_counts():
    pts, c = _select({
        "Revenues": {"USD": [_rev(1, "2024-06-30", "A1")], "EUR": [_rev(2, "2023-06-30", "A1")]},
        "CommonStockSharesOutstanding": {"shares": [_inst(7, "2024-06-30")]},
    })
    assert [p.unit for p in pts] == ["shares"] and c["dropped:currency_ambiguous"] == 2


def test_the_ambiguity_of_one_filing_does_not_touch_another():
    pts, _ = _select({"Revenues": {
        "USD": [_rev(1, "2024-06-30", "A1"), _rev(5, "2023-06-30", "B1")],
        "EUR": [_rev(2, "2022-06-30", "A1")]}})
    assert [(p.accn, p.unit) for p in pts] == [("B1", "USD")]


@pytest.mark.parametrize("reverse", [False, True])
def test_the_currency_never_depends_on_fact_order(reverse):
    facts = [("USD", _rev(1, "2024-06-30", "A1")), ("EUR", _rev(2, "2023-06-30", "A1")),
             ("EUR", _rev(3, "2022-06-30", "A1"))]
    units = {}
    for unit, p in (reversed(facts) if reverse else facts):
        units.setdefault(unit, []).append(p)
    pts, _ = _select({"Revenues": units})
    assert {p.unit for p in pts} == {"EUR"} and len(pts) == 2


# --- Per-share values of a filing with no monetary fact.


def _eps(units, accn="A1"):
    return {"EarningsPerShareBasic": {u: [point(1, "2024-06-30", start="2023-07-01", accn=accn)]
                                      for u in units}}


def test_a_per_share_unit_without_an_iso_numerator_never_publishes():
    pts, c = _select(_eps(["pure/shares"]))
    assert pts == [] and c["dropped:unexpected_unit"] == 1


def test_a_single_per_share_currency_publishes_without_any_monetary_fact():
    pts, c = _select(_eps(["CAD/shares"]))
    assert [p.unit for p in pts] == ["CAD/shares"] and c["dropped:currency_ambiguous"] == 0


def test_two_per_share_currencies_in_a_filing_without_money_are_not_guessed():
    # Equal values in both: the choice would be arbitrary and, once published, permanent.
    pts, c = _select(_eps(["USD/shares", "CAD/shares"]))
    assert pts == [] and c["dropped:currency_ambiguous"] == 2


def test_two_per_share_currencies_do_not_matter_when_the_filing_has_money():
    us_gaap = _eps(["USD/shares", "CAD/shares"])
    us_gaap["Revenues"] = {"USD": [_rev(1, "2024-06-30", "A1")]}
    pts, c = _select(us_gaap)
    assert {p.unit for p in pts} == {"USD", "USD/shares"} and c["dropped:currency_ambiguous"] == 0


def test_per_share_ambiguity_is_a_function_of_the_filing_alone():
    us_gaap = _eps(["USD/shares", "CAD/shares"], accn="A1")
    us_gaap["EarningsPerShareDiluted"] = {"EUR/shares": [point(2, "2024-06-30", start="2023-07-01",
                                                               accn="B1")]}
    pts, _ = _select(us_gaap)
    assert [(p.accn, p.unit) for p in pts] == [("B1", "EUR/shares")]


# --- A bad unit shape is reported as such, whatever the filing's currency signals.


def test_a_bad_unit_shape_is_unexpected_unit_even_in_an_ambiguous_filing():
    pts, c = _select({
        "Revenues": {"USD": [_rev(1, "2024-06-30", "A1")], "EUR": [_rev(2, "2023-06-30", "A1")]},
        "NetIncomeLoss": {"pure": [_rev(3, "2024-06-30", "A1")]},
        "EarningsPerShareBasic": {"pure/shares": [_rev(4, "2024-06-30", "A1")]},
    })
    assert pts == [] and c["dropped:currency_ambiguous"] == 2
    assert c["dropped:unexpected_unit"] == 2
