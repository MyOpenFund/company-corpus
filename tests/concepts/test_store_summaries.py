"""Store rows -> summaries must give F1's values and ratios (spec MCP-2 Q5, Q6)."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pyarrow as pa
import pytest

from company_corpus.concepts.selection import run_selection
from company_corpus.concepts.store_summaries import (
    concept_catalog, metric_catalog, summaries_from_rows)
from company_corpus.concepts.summaries import (
    POSITION_SCHEMA, attach_ttm, build_summaries, positions_table)
from company_corpus.config import Config
from company_corpus.opendata.fundamentals import fundamentals_rows

from .support import FIXTURES, fixture

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
#: Every trimmed real issuer in tests/fixtures/concepts (calendar and off-calendar year-ends,
#: quarters and annual-only, banks, insurers, REITs).
FIXTURE_NAMES = sorted(p.stem for p in FIXTURES.glob("*.json"))


def vintages(name: str) -> list[dict]:
    """What the store would hold for this issuer: the producer's own fundamentals rows."""
    fx = fixture(name)
    return fundamentals_rows(str(fx["cik"]).zfill(10), fx["facts"], acceptance={},
                             manifest_doc_ids=(), config=Config(), now=NOW).rows


def store_rows(name: str) -> list[dict]:
    """The selection a store reader runs on the issuer's vintages and statement positions."""
    fx = fixture(name)
    cik = str(fx["cik"]).zfill(10)
    pos = positions_table(fx["statements"]).to_pylist()
    for p in pos:
        p["entity_value"] = cik
    return run_selection(pa.Table.from_pylist(vintages(name)),
                         pa.Table.from_pylist(pos, schema=POSITION_SCHEMA))


def store_summaries(name: str, **kw):
    return summaries_from_rows(store_rows(name), company="x", vintages=vintages(name), **kw)


def by_period(summaries):
    return {(s.period_end, s.frequency): s for s in summaries}


def _same(a, b, rel) -> bool:
    return a is None and b is None or (a is not None and b is not None
                                       and a == pytest.approx(b, rel=rel))


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_store_summaries_equal_f1(name):
    """Spec MCP-2 Q5: same periods, values, ratios (point-in-time and TTM), publication date,
    accession and currency as F1 under the same Q4 rule. (F1's default ytd9 rule differs on
    quarterly TTM only by rounding: see the oracle test below; annual TTM does not use it.)"""
    fx = fixture(name)
    f1_list = build_summaries(fx["facts"], company="x", company_current="x",
                              statements=fx["statements"])
    attach_ttm(fx["facts"], f1_list, statements=fx["statements"], q4_rule="sum_of_quarters")
    f1, store = by_period(f1_list), by_period(store_summaries(name))
    assert store.keys() == f1.keys() and f1, name
    for k, s in store.items():
        f = f1[k]
        assert (s.publication_date, s.accession, s.currency) == \
               (f.publication_date, f.accession, f.currency), (name, k)
        assert {c: v["value"] for c, v in s.values.items()} == \
               {c: v["value"] for c, v in f.values.items()}, (name, k)
        assert s.derived.keys() == f.derived.keys(), (name, k)
        for m, v in s.derived.items():
            assert _same(v["value"], f.derived[m]["value"], 1e-9), (name, k, m)
        assert s.ttm.keys() == f.ttm.keys(), (name, k)
        for m, v in s.ttm.items():
            assert _same(v["value"], f.ttm[m]["value"], 1e-6), (name, k, m)


def _quarterly_ttm(summaries) -> dict:
    return {(x.period_end, m): v["value"] for x in summaries if x.frequency == "quarterly"
            for m, v in x.ttm.items() if v["value"] is not None}


def test_quarterly_ttm_needs_sum_of_quarters_on_store_rows():
    """oracle (May year-end): its FY2022 Q4 net income is not reported standalone; the store has
    no 9-month band, so only sum_of_quarters rebuilds it and gives the quarterly net margin."""
    rows = store_rows("oracle")

    def margins(rule):
        return {k for k in _quarterly_ttm(summaries_from_rows(rows, company="x", q4_rule=rule))
                if k[1] == "net_margin_ttm"}
    assert len(margins("sum_of_quarters")) == 4
    assert not margins("ytd9")


def test_quarterly_ttm_matches_f1_under_the_same_q4_rule():
    """Same Q4 rule, same numbers. Against F1's own ytd9 rule the quarterly TTM of the windows
    holding FY2022 Q4 differs by Oracle's rounding: Q1-Q3 FY2022 are each rounded to $1M and sum
    to $1M more than the reported 9-month YTD (revenue 30,601 vs 30,600; net income 3,529 vs
    3,528), so Q4 = FY - YTD9 and Q4 = FY - (Q1+Q2+Q3) differ by $1M (relative ~1e-4)."""
    fx = fixture("oracle")
    store = _quarterly_ttm(summaries_from_rows(store_rows("oracle"), company="x"))
    by_rule = {}
    for rule in ("sum_of_quarters", "ytd9"):
        f1 = build_summaries(fx["facts"], company="x", company_current="x",
                             statements=fx["statements"])
        attach_ttm(fx["facts"], f1, statements=fx["statements"], q4_rule=rule)
        by_rule[rule] = _quarterly_ttm(f1)
    assert store and store.keys() <= by_rule["sum_of_quarters"].keys()
    for k, v in store.items():
        assert v == pytest.approx(by_rule["sum_of_quarters"][k], rel=1e-9), k
    # The expected, explained divergence from F1's ytd9 rule: rounding only, never more.
    differ = {k for k, v in store.items() if v != pytest.approx(by_rule["ytd9"][k], rel=1e-9)}
    assert differ and {e for e, _ in differ} <= {date(2022, 5, 31), date(2022, 8, 31),
                                                  date(2022, 11, 30), date(2023, 2, 28)}
    for k in differ:
        assert store[k] == pytest.approx(by_rule["ytd9"][k], rel=5e-4), k


def _row(concept, end, frequency, known_at, value=1):
    return {"concept": concept, "valid_date": end, "frequency": frequency, "known_at": known_at,
            "value": Decimal(value), "unit": "USD", "original_tag": f"us-gaap:{concept}",
            "location": None, "conflict": False, "on_statement": True, "filing_ref": "A"}


@pytest.mark.parametrize("known_at", [
    datetime(2025, 2, 14, 4, 59, 59, tzinfo=timezone.utc),  # 23:59:59 New York, aware
    datetime(2025, 2, 14, 4, 59, 59),                        # the same instant, naive UTC
])
def test_publication_date_is_the_new_york_filing_day(known_at):
    rows = [_row("revenue", date(2024, 12, 31), "annual", known_at)]
    (s,) = summaries_from_rows(rows, company="x")
    assert s.publication_date == date(2025, 2, 13)
    (s,) = summaries_from_rows(rows, company="x", publication_tz="UTC")
    assert s.publication_date == date(2025, 2, 14)


def test_publication_date_is_the_first_filing_not_the_selected_vintage():
    """equinix: its 2026-10-28 10-Q re-reports Q1 2025 and Q1 2026, first filed on 2026-07-29.
    The selection keeps the latest vintage per tag and period, so the publication date and
    accession come from the vintages, as F1 takes them from every as-filed point (F1 parity
    itself is in test_store_summaries_equal_f1)."""
    store = by_period(store_summaries("equinix"))
    q1 = store[(date(2025, 3, 31), "quarterly")]
    assert (q1.publication_date, q1.accession) == (date(2026, 7, 29), "0001101239-26-000147")
    without = by_period(summaries_from_rows(store_rows("equinix"), company="x"))
    assert without[(date(2025, 3, 31), "quarterly")].publication_date == date(2026, 10, 28)


@pytest.mark.parametrize("other", [[], "jpmorgan"])
def test_vintages_of_no_selected_row_are_refused(other):
    """An empty list, or another issuer's vintages, would silently drop every period."""
    wrong = vintages(other) if other else []
    with pytest.raises(ValueError, match="vintages hold no row"):
        summaries_from_rows(store_rows("caterpillar"), company="x", vintages=wrong)


def test_unknown_sector_leaves_relevance_null():
    s = summaries_from_rows(store_rows("jpmorgan"), company="x", sector_known=False)
    rel = {v["sector_relevant"] for x in s for v in x.derived.values()}
    assert None in rel and False not in rel


def test_empty_rows_give_no_summary():
    assert summaries_from_rows([], company="x") == []


def test_catalogs():
    concepts = {c["key"]: c for c in concept_catalog()}
    assert {"revenue", "net_income", "assets"} <= concepts.keys()
    assert {c["kind"] for c in concepts.values()} <= {"money", "money/shares", "shares"}
    assert concepts["revenue"]["kind"] == "money" and "unit" not in concepts["revenue"]
    metrics = {m["key"]: m for m in metric_catalog()}
    assert metrics["roe_ttm"]["ttm"] is True and metrics["net_margin"]["unit"] == "%"


def test_a_period_only_seen_before_it_ended_has_no_summary():
    """As in F1: a filing can hold a fact about a later period (a declared dividend)."""
    early = datetime(2024, 11, 1, 21, tzinfo=timezone.utc)
    rows = [_row("revenue", date(2024, 12, 31), "annual", early)]
    assert summaries_from_rows(rows, company="x") == []
    vintage = {"original_tag": "us-gaap:Revenues", "valid_date": date(2024, 12, 31),
               "frequency": "annual", "known_at": early, "filing_ref": "A"}
    assert summaries_from_rows(rows, company="x", vintages=[vintage]) == []


def test_mixed_naive_and_aware_known_at_do_not_clash():
    """A naive known_at is UTC: comparable with aware ones, earliest filing wins."""
    end = date(2024, 12, 31)
    rows = [_row("revenue", end, "annual", datetime(2025, 2, 14, 21, tzinfo=timezone.utc)),
            {**_row("assets", end, "instant", datetime(2025, 2, 13, 21)), "filing_ref": "B"}]
    (s,) = summaries_from_rows(rows, company="x")
    assert (s.publication_date, s.accession) == (date(2025, 2, 13), "B")
