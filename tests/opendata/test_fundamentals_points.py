from collections import Counter
from datetime import date
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


def _transitions(us_gaap, dei=None):
    ends = {}
    select_points(flatten_with_namespace(companyfacts(us_gaap, dei)),
                  DEFAULT_DURATION_BANDS, Counter(), ends)
    return ends


def test_transition_end_is_the_max_duration_end_per_10kt_accession():
    ends = _transitions({"Revenues": {"USD": [
        point(1, "2023-12-31", start="2023-01-01", accn="a-1", form="10-KT"),
        point(2, "2024-06-30", start="2024-01-01", accn="a-1", form="10-KT"),
        point(3, "2024-03-31", start="2024-01-01", accn="a-1", form="10-KT"),
        point(4, "2025-06-30", start="2025-01-01", accn="a-2", form="10-KT"),
        point(5, "2025-09-30", start="2025-01-01", accn="a-3", form="10-K"),
    ]}})
    assert ends == {"a-1": date(2024, 6, 30), "a-2": date(2025, 6, 30)}


def test_10kt_amendment_is_recorded_too():
    ends = _transitions({"Revenues": {"USD": [
        point(1, "2024-06-30", start="2023-08-01", accn="a-1", form="10-KT/A")]}})
    assert ends == {"a-1": date(2024, 6, 30)}


def test_dropped_odd_duration_10kt_point_is_still_recorded():
    ends = _transitions({"Revenues": {"USD": [
        point(1, "2024-06-30", start="2023-08-01", accn="a-1", form="10-KT")]}})  # 335 days
    assert ends == {"a-1": date(2024, 6, 30)}


def test_10kt_instant_point_is_not_recorded():
    ends = _transitions({"LongTermDebt": {"USD": [point(1, "2024-06-30", accn="a-1", form="10-KT")]}})
    assert ends == {}


def test_10kt_duration_point_in_a_non_reporting_currency_is_still_recorded():
    ends = _transitions({"Revenues": {
        "USD": [point(v, f"2024-0{v}-28", start=f"2024-0{v - 1}-01", accn="a-9", form="10-Q")
                for v in (3, 4, 5)],
        "EUR": [point(1, "2024-06-30", start="2023-08-01", accn="a-1", form="10-KT")],
    }})
    assert ends["a-1"] == date(2024, 6, 30)


def test_10kt_point_with_an_unparseable_end_is_not_recorded():
    ends = _transitions({"Revenues": {"USD": [
        point(1, "garbage", start="2023-08-01", accn="a-1", form="10-KT")]}})
    assert ends == {}
