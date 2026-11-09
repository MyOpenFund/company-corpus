"""IFRS filers through as-filed ingestion (chantier 10 spec §4)."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from company_corpus.config import DEFAULT_CURRENCY_ANCHOR_TAGS, DEFAULT_DURATION_BANDS, Config
from company_corpus.opendata.fundamentals import (
    as_filed_points,
    flatten_with_namespace,
    fundamentals_rows,
    ingest_namespaces,
)
from tests.concepts.tables import companyfacts, with_test_ifrs

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
A, B = "0000000001-25-000001", "0000000001-25-000002"


def _p(unit, val, end, accn, *, start=None, form="20-F", filed="2025-03-01"):
    p = {"unit": unit, "val": val, "end": end, "accn": accn, "form": form, "filed": filed}
    if start:
        p["start"] = start
    return p


def _rows(facts, d):
    return fundamentals_rows("1", facts, acceptance={}, manifest_doc_ids=set(), config=Config(),
                             now=NOW, dictionary=d)


def test_namespaces_are_the_tables_then_the_cover_page():
    assert ingest_namespaces(with_test_ifrs()) == ("us-gaap", "ifrs-full", "dei")


def test_flattening_keeps_both_namespaces_of_one_bare_tag():
    facts = companyfacts({"us-gaap:Assets": [_p("USD", 1, "2024-12-31", A, form="10-K")],
                          "ifrs-full:Assets": [_p("USD", 2, "2023-12-31", B)]})
    flat = flatten_with_namespace(facts, ("us-gaap", "ifrs-full", "dei"))
    assert set(flat) == {"us-gaap:Assets", "ifrs-full:Assets"}
    assert flat["ifrs-full:Assets"][0]["ns"] == "ifrs-full"


def test_a_gbp_ifrs_filer_gets_rows_as_filed():
    d = with_test_ifrs()
    facts = companyfacts({
        "ifrs-full:Revenue": [_p("GBP", 14000, "2024-12-31", A, start="2024-01-01")],
        "ifrs-full:Assets": [_p("GBP", 700000, "2024-12-31", A)],
        "ifrs-full:BasicEarningsLossPerShare": [_p("GBP/shares", "0.5", "2024-12-31", A,
                                                   start="2024-01-01")]})
    res = _rows(facts, d)
    got = {r["original_tag"]: (r["value"], r["unit"]) for r in res.rows}
    assert got == {"ifrs-full:Revenue": (Decimal("14000.000000"), "GBP"),
                   "ifrs-full:Assets": (Decimal("700000.000000"), "GBP"),
                   "ifrs-full:BasicEarningsLossPerShare": (Decimal("0.500000"), "GBP/shares")}


def test_a_convenience_translation_is_not_a_second_fact():
    d = with_test_ifrs()
    facts = companyfacts({
        "ifrs-full:Revenue": [_p("USD", 100, "2024-12-31", A, start="2024-01-01"),
                              _p("CHF", 90, "2024-12-31", A, start="2024-01-01")],
        "ifrs-full:Assets": [_p("USD", 1000, "2024-12-31", A), _p("USD", 900, "2023-12-31", A)]})
    res = _rows(facts, d)
    assert {(r["original_tag"], r["unit"]) for r in res.rows} == {
        ("ifrs-full:Revenue", "USD"), ("ifrs-full:Assets", "USD")}
    assert res.counters["dropped:non_reporting_currency"] == 1


def test_the_same_bare_tag_in_two_namespaces_gives_two_rows():
    d = with_test_ifrs()
    facts = companyfacts({"us-gaap:Assets": [_p("USD", 10, "2024-12-31", B, form="10-K",
                                                 filed="2025-02-01")],
                          "ifrs-full:Assets": [_p("USD", 11, "2023-12-31", A)]})
    tags = sorted(r["original_tag"] for r in _rows(facts, d).rows)
    assert tags == ["ifrs-full:Assets", "us-gaap:Assets"]


def test_each_filing_votes_on_its_own_facts_at_a_filer_that_switched():
    d = with_test_ifrs()
    facts = companyfacts({
        "ifrs-full:Assets": [_p("EUR", 50, "2022-12-31", A, filed="2023-03-01")],
        "ifrs-full:Revenue": [_p("EUR", 5, "2022-12-31", A, start="2022-01-01", filed="2023-03-01")],
        "us-gaap:Assets": [_p("USD", 60, "2023-12-31", B, form="10-K", filed="2024-03-01")]})
    units = {r["original_tag"]: r["unit"] for r in _rows(facts, d).rows}
    assert units == {"ifrs-full:Assets": "EUR", "ifrs-full:Revenue": "EUR", "us-gaap:Assets": "USD"}


def test_a_tag_outside_every_table_is_not_ingested():
    d = with_test_ifrs()
    facts = companyfacts({"ifrs-full:GrossProfit": [_p("GBP", 1, "2024-12-31", A,
                                                       start="2024-01-01")],
                          "ifrs-full:Assets": [_p("GBP", 2, "2024-12-31", A)]})
    assert [r["original_tag"] for r in _rows(facts, d).rows] == ["ifrs-full:Assets"]


def test_anchor_tags_are_full_tags_for_both_taxonomies():
    assert DEFAULT_CURRENCY_ANCHOR_TAGS == (
        "us-gaap:Assets", "us-gaap:Liabilities", "us-gaap:StockholdersEquity",
        "us-gaap:LiabilitiesAndStockholdersEquity", "ifrs-full:Assets", "ifrs-full:Liabilities",
        "ifrs-full:Equity", "ifrs-full:EquityAndLiabilities")


@pytest.mark.parametrize("tags", [("Assets",), ("us-gaap:",), (":Assets",)])
def test_a_bare_anchor_tag_is_refused(tags):
    with pytest.raises(ValueError, match="store_currency_anchor_tags"):
        Config(store_currency_anchor_tags=tags)


def test_us_points_do_not_move_when_the_ifrs_table_is_loaded():
    from company_corpus.concepts.dictionary import load_dictionary
    from tests.concepts.support import fixture

    fx = fixture("marriott")
    us_only = _rows(fx["facts"], load_dictionary(("us_gaap",))).rows
    both = _rows(fx["facts"], with_test_ifrs()).rows
    assert both == us_only
