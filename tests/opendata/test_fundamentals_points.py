from collections import Counter
from decimal import Decimal

import pytest

from company_corpus.config import DEFAULT_DURATION_BANDS
from company_corpus.opendata.fundamentals import (
    classify_duration,
    flatten_with_namespace,
    select_points,
)
from tests.opendata.factories import companyfacts, point


def _select(us_gaap, dei=None):
    counters = Counter()
    pts = select_points(flatten_with_namespace(companyfacts(us_gaap, dei)),
                        DEFAULT_DURATION_BANDS, counters)
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
    got = sorted((p.concept, p.frequency, p.value) for p in pts)
    assert got == [
        ("long_term_debt", "instant", Decimal(100)),
        ("revenue", "annual", Decimal(40)),
        ("revenue", "quarterly", Decimal(10)),
        ("revenue", "semiannual", Decimal(20)),
    ]
    assert c["dropped:ytd9"] == 1 and c["dropped:odd_duration"] == 1


def test_first_tag_of_the_chain_wins_per_filing_and_period():
    pts, _ = _select({
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": [
            point(100, "2024-06-30", start="2023-07-01")]},
        "Revenues": {"USD": [
            point(101, "2024-06-30", start="2023-07-01"),                       # same filing
            point(90, "2023-06-30", start="2022-07-01", accn="0000000042-23-000001")]},
    })
    got = sorted((p.end.isoformat(), p.tag, p.value) for p in pts)
    assert got == [
        ("2023-06-30", "Revenues", Decimal(90)),          # only tag present for that period
        ("2024-06-30", "RevenueFromContractWithCustomerExcludingAssessedTax", Decimal(100)),
    ]


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
    assert sorted((p.concept, p.unit) for p in pts) == [
        ("eps_diluted", "USD/shares"), ("revenue", "USD"), ("revenue", "USD"), ("revenue", "USD")]
    assert c["dropped:non_reporting_currency"] == 2 and c["dropped:unexpected_unit"] == 1


def test_dei_fallback_tag_keeps_its_namespace():
    pts, _ = _select({}, dei={"EntityCommonStockSharesOutstanding": {
        "shares": [point(7426194552, "2026-10-22", form="10-Q")]}})
    (p,) = pts
    assert (p.concept, p.ns, p.tag, p.frequency) == (
        "shares_outstanding", "dei", "EntityCommonStockSharesOutstanding", "instant")


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


@pytest.mark.parametrize("order", [("EUR", "GBP"), ("GBP", "EUR")])
def test_a_currency_tie_does_not_depend_on_tag_order(order):
    us_gaap = {tag: {unit: [_rev(i + 1, "2023-12-31", "A1")]}
               for i, (tag, unit) in enumerate(zip(("Revenues", "NetIncomeLoss"), order))}
    pts, _ = _select(us_gaap)
    assert {p.unit for p in pts} == {"EUR"}


def test_a_currency_tie_with_usd_goes_to_usd():
    pts, _ = _select({"Revenues": {"EUR": [_rev(1, "2023-12-31", "A1")]},
                      "NetIncomeLoss": {"USD": [_rev(2, "2023-12-31", "A1")]}})
    assert {p.unit for p in pts} == {"USD"}
