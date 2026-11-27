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


@pytest.mark.parametrize("q4_rule", ["ytd9", "sum_of_quarters"])
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_store_summaries_equal_f1(name, q4_rule):
    """Spec MCP-2 Q5: same periods, values, ratios (point-in-time and TTM), publication date,
    accession and currency as F1 under the same Q4 rule, for both rules (annual TTM does not use
    the Q4 rule)."""
    fx = fixture(name)
    f1_list = build_summaries(fx["facts"], company="x", company_current="x",
                              statements=fx["statements"])
    attach_ttm(fx["facts"], f1_list, statements=fx["statements"], q4_rule=q4_rule)
    f1, store = by_period(f1_list), by_period(store_summaries(name, q4_rule=q4_rule))
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


def _f1_quarterly_ttm(name, rule) -> dict:
    fx = fixture(name)
    f1 = build_summaries(fx["facts"], company="x", company_current="x",
                         statements=fx["statements"])
    attach_ttm(fx["facts"], f1, statements=fx["statements"], q4_rule=rule)
    return _quarterly_ttm(f1)


def test_quarterly_ttm_equals_f1_under_ytd9_on_store_rows():
    """The store carries the 9-month YTD band (fundamentals 3.1.0), so the default Q4 rule is F1's
    ytd9 and store-row quarterly TTM equals F1's exactly, including the windows holding oracle's
    FY2022 Q4 (not reported standalone). The store must carry ytd9 because the two rules can
    really differ when the quarterly and the year-to-date lines come from different statements
    (seen on real data, not in these fixtures)."""
    store = _quarterly_ttm(summaries_from_rows(store_rows("oracle"), company="x"))
    f1 = _f1_quarterly_ttm("oracle", "ytd9")
    assert store and store.keys() == f1.keys()
    assert any(e == date(2022, 5, 31) and m == "net_margin_ttm" for e, m in store)
    for k, v in store.items():
        assert v == pytest.approx(f1[k], rel=1e-9), k


def test_quarterly_ttm_equals_f1_under_sum_of_quarters_on_store_rows():
    """sum_of_quarters stays a parameter: store rows under it equal F1 run with the same rule."""
    store = _quarterly_ttm(summaries_from_rows(store_rows("oracle"), company="x",
                                               q4_rule="sum_of_quarters"))
    f1 = _f1_quarterly_ttm("oracle", "sum_of_quarters")
    assert store and store.keys() == f1.keys()
    for k, v in store.items():
        assert v == pytest.approx(f1[k], rel=1e-9), k


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


# ---- chantier 12: derived quarters in both summary paths (spec rules 7, 8, 9, D4) ----

def _f1(name, q4_rule="ytd9"):
    fx = fixture(name)
    out = build_summaries(fx["facts"], company="x", company_current="x",
                          statements=fx["statements"])
    attach_ttm(fx["facts"], out, statements=fx["statements"], q4_rule=q4_rule)
    return out


def test_store_summary_q2_has_derived_capex_and_fcf():
    """oracle files its cash flows year to date: its fiscal Q2 (2022-11-30) capex is 6M - Q1,
    and the free cash flow resting on it is computed."""
    q2 = by_period(store_summaries("oracle"))[(date(2022, 11, 30), "quarterly")]
    assert q2.values["capex"]["derived"] == "6M - Q1"
    assert q2.values["capex"]["value"] == 4154000000 - 1719000000
    assert q2.values["capex"]["tag"] == "PaymentsToAcquirePropertyPlantAndEquipment"
    assert q2.values["revenue"]["derived"] is None
    assert q2.derived["free_cash_flow"]["value"] == (7243000000 - 6394000000) - 2435000000


@pytest.mark.parametrize("path", ["store", "f1"])
def test_store_summary_q4_dated_by_10k(path):
    """A derived Q4 creates a quarterly summary at the fiscal year-end, dated and accessioned by
    the 10-K (oracle FY2023, 10-K 0000950170-23-028914 filed 2023-06-20)."""
    summaries = store_summaries("oracle") if path == "store" else _f1("oracle")
    q4 = by_period(summaries)[(date(2023, 5, 31), "quarterly")]
    fy = by_period(summaries)[(date(2023, 5, 31), "annual")]
    assert q4.values["capex"]["derived"] == "FY - 9M"
    assert q4.values["capex"]["value"] == 8695000000 - 6782000000
    assert (q4.publication_date, q4.accession) == (date(2023, 6, 20), "0000950170-23-028914")
    assert (q4.publication_date, q4.accession) == (fy.publication_date, fy.accession)
    if path == "f1":
        assert q4.sec_form == fy.sec_form == "10-K"


def test_store_q4_publication_without_vintages_is_the_10k_too():
    q4 = by_period(summaries_from_rows(store_rows("oracle"), company="x"))[
        (date(2023, 5, 31), "quarterly")]
    assert q4.accession == "0000950170-23-028914"


@pytest.mark.parametrize("path", ["store", "f1"])
def test_quarterly_ttm_at_q2_now_computable(path):
    """YTD filer: the TTM FCF margin at fiscal Q2 needs the Q2 and Q3 capex, derived. Before
    chantier 12 (main) it was None."""
    summaries = store_summaries("oracle") if path == "store" else _f1("oracle")
    q2 = by_period(summaries)[(date(2022, 11, 30), "quarterly")]
    assert q2.ttm["fcf_margin_ttm"]["value"] is not None


_TAGS = {"revenue": "us-gaap:Revenues", "net_income": "us-gaap:NetIncomeLoss"}


def _standalone_rows() -> list[dict]:
    """A standalone-quarter filer over two fiscal years (Q1-Q3 filed, 9M and FY filed): only its
    Q4s are derived (D4). The 9M and FY figures deliberately disagree with the sum of the
    quarters, so ytd9 and sum_of_quarters give different Q4s."""
    out = []

    def add(concept, freq, start, end, value, filed):
        out.append({"concept": concept, "entity_scheme": "cik", "entity_value": "1",
                    "basis": "consolidated", "source_code": "sec", "frequency": freq,
                    "period_start": date.fromisoformat(start), "valid_date": date.fromisoformat(end),
                    "value": Decimal(value), "unit": "USD", "original_tag": _TAGS[concept],
                    "location": "IS", "conflict": False, "on_statement": True,
                    "filing_ref": filed, "source_doc_id": None,
                    "known_at": datetime.fromisoformat(filed).replace(hour=21, tzinfo=timezone.utc)})

    for y, base in ((2023, 100), (2024, 200)):
        add("revenue", "quarterly", f"{y}-01-01", f"{y}-03-31", base, f"{y}-05-01")
        add("revenue", "quarterly", f"{y}-04-01", f"{y}-06-30", base + 1, f"{y}-08-01")
        add("revenue", "quarterly", f"{y}-07-01", f"{y}-09-30", base + 2, f"{y}-11-01")
        add("revenue", "ytd9", f"{y}-01-01", f"{y}-09-30", 3 * base + 4, f"{y}-11-01")
        add("revenue", "annual", f"{y}-01-01", f"{y}-12-31", 4 * base + 10, f"{y + 1}-02-01")
        for end, freq, value, filed in (("03-31", "quarterly", 10, "05-01"),
                                        ("06-30", "quarterly", 10, "08-01"),
                                        ("09-30", "quarterly", 10, "11-01")):
            start = {"03-31": "01-01", "06-30": "04-01", "09-30": "07-01"}[end]
            add("net_income", freq, f"{y}-{start}", f"{y}-{end}", value, f"{y}-{filed}")
        add("net_income", "ytd9", f"{y}-01-01", f"{y}-09-30", 30, f"{y}-11-01")
        add("net_income", "annual", f"{y}-01-01", f"{y}-12-31", 40, f"{y + 1}-02-01")
    return out


@pytest.mark.parametrize("q4_rule", ["ytd9", "sum_of_quarters"])
def test_sum_of_quarters_unchanged_for_standalone_filer(q4_rule):
    """Spec rule 7: for a standalone filer TTM is main's under both rules: main's TTM is
    _attach_ttm on the rows without derived quarters (the derived Q4 never reaches TTM)."""
    from company_corpus.concepts.summaries import _flow_series
    from company_corpus.financials import _TTM_FLOW_KEYS, _attach_ttm

    rows = _standalone_rows()
    summaries = summaries_from_rows(rows, company="x", q4_rule=q4_rule)
    got = {(s.period_end, s.frequency): s.ttm for s in summaries}
    # Revenue Q4 2023 = FY - 9M = 410 - 304 = 106 (ytd9) or FY - Q1..Q3 = 410 - 303 = 107;
    # net income Q4 = 10 under both. TTM at Q3 2024 = Q4 2023 + Q1..Q3 2024.
    q4 = 106 if q4_rule == "ytd9" else 107
    q3 = got[(date(2024, 9, 30), "quarterly")]
    assert q3["net_margin_ttm"]["value"] == pytest.approx(40 / (q4 + 603) * 100)
    q4_summary = by_period(summaries)[(date(2024, 12, 31), "quarterly")]
    assert q4_summary.values["revenue"]["derived"] == "FY - 9M"  # D4: a derived Q4 summary
    _attach_ttm(_flow_series(rows), summaries, ttm_keys=_TTM_FLOW_KEYS, q4_rule=q4_rule)
    assert {(s.period_end, s.frequency): s.ttm for s in summaries} == got


def test_derived_q4_alone_dates_its_summary():
    """No instant and no filed quarter at the fiscal year-end: only the derived Q4 rows are
    publication-date candidates, on both paths (store with and without vintages, F1)."""
    rows = _standalone_rows()
    for vintages_ in (rows, None):
        q4 = by_period(summaries_from_rows(rows, company="x", vintages=vintages_))[
            (date(2024, 12, 31), "quarterly")]
        assert (q4.publication_date, q4.accession) == (date(2025, 2, 1), "2025-02-01")

    facts: dict = {"facts": {"us-gaap": {}}}
    for r in rows:
        facts["facts"]["us-gaap"].setdefault(r["original_tag"].split(":")[1], {"units": {"USD": []}})[
            "units"]["USD"].append({
                "start": r["period_start"].isoformat(), "end": r["valid_date"].isoformat(),
                "val": int(r["value"]), "accn": r["filing_ref"],
                "form": "10-K" if r["frequency"] == "annual" else "10-Q",
                "filed": r["filing_ref"]})
    f1 = by_period(build_summaries(facts, company="x", company_current="x"))
    q4 = f1[(date(2024, 12, 31), "quarterly")]
    assert (q4.publication_date, q4.accession, q4.sec_form) == (
        date(2025, 2, 1), "2025-02-01", "10-K")
    assert q4.values["revenue"]["derived"] == "FY - 9M"


@pytest.mark.parametrize("q4_rule", ["ytd9", "sum_of_quarters"])
@pytest.mark.parametrize("name", ["caterpillar", "echostar_early", "jbs", "lilly_early"])
def test_standalone_fixtures_ttm_unchanged(name, q4_rule):
    """Real standalone filers (no Q2/Q3 derived): TTM on both paths equals main's."""
    from company_corpus.concepts.summaries import _flow_series, select_for_f1
    from company_corpus.financials import _TTM_FLOW_KEYS, _attach_ttm

    fx = fixture(name)
    for summaries, rows in ((_f1(name, q4_rule), select_for_f1(fx["facts"], fx["statements"]).rows),
                            (store_summaries(name, q4_rule=q4_rule), store_rows(name))):
        got = {(s.period_end, s.frequency): s.ttm for s in summaries}
        _attach_ttm(_flow_series(rows), summaries, ttm_keys=_TTM_FLOW_KEYS, q4_rule=q4_rule)
        assert {(s.period_end, s.frequency): s.ttm for s in summaries} == got


def test_f1_path_matches_store_path():
    """Spec rule 9: one computation. The derived values (and their formulas) of F1 and of the
    store path are the same on the companyfacts fixture."""
    def marked(summaries):
        return {(s.period_end, s.frequency, k): (v["value"], v["derived"])
                for s in summaries for k, v in s.values.items() if v["derived"] is not None}

    f1, store = marked(_f1("oracle")), marked(store_summaries("oracle"))
    assert f1 and f1 == store
    assert {d for _, d in f1.values()} == {"6M - Q1", "9M - 6M", "FY - 9M"}
