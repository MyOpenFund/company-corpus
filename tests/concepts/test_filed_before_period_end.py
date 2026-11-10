"""Facts filed before their period ended (chantier 11, Backlog 19): dropped per concept, in the
selection the store readers and F1 share."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from importlib import resources

import pyarrow as pa
import pytest

from company_corpus.concepts.dictionary import parse_dictionary
from company_corpus.concepts.selection import DEFAULT_FILING_TZ, run_selection, selection_sql
from company_corpus.concepts.summaries import (
    FACT_SCHEMA,
    POSITION_SCHEMA,
    build_summaries,
    group_periods,
    select_for_f1,
)
from company_corpus.concepts.store_summaries import summaries_from_rows

from .support import fixture, pick

UTC = timezone.utc
Q1 = date(2014, 3, 31)
K_FILED = datetime(2014, 2, 19, 23, 59, 59, tzinfo=UTC)   # annual report, before Q1 ended
Q_FILED = datetime(2014, 4, 28, 23, 59, 59, tzinfo=UTC)   # Q1 report, after


def _fact(tag, value, filing, known_at, *, end=Q1, start=None, frequency="instant", unit="USD"):
    return {"entity_scheme": "cik", "entity_value": "1", "original_tag": tag,
            "period_start": start, "frequency": frequency, "basis": "consolidated",
            "value": Decimal(value), "unit": unit, "filing_ref": filing, "source_doc_id": None,
            "known_at": known_at, "valid_date": end, "source_code": "sec"}


def _position(filing, tag, known_at, statement="BS", version="us-gaap/2013"):
    return {"entity_scheme": "cik", "entity_value": "1", "filing_ref": filing,
            "statement": statement, "tag": tag, "tag_version": version, "known_at": known_at}


def _select(facts, positions=(), **kw):
    return run_selection(pa.Table.from_pylist(list(facts), schema=FACT_SCHEMA),
                         pa.Table.from_pylist(list(positions), schema=POSITION_SCHEMA), **kw)


def _value(rows, concept, day=Q1):
    found = [r for r in rows if r["concept"] == concept and r["valid_date"] == day]
    assert len(found) <= 1, found
    return found[0]["value"] if found else None


def test_a_balance_only_filed_before_its_date_has_no_value():
    rows = _select([_fact("us-gaap:ShortTermBorrowings", 1000, "K", K_FILED)])
    assert _value(rows, "short_term_debt") is None


def test_an_early_statement_line_no_longer_beats_an_on_time_note():
    """Two tags of one concept, so ``facts_in`` (per tag) keeps both and only the concept rule
    decides. Before chantier 11 the early fact won: on the statement (tier 0) beats a note
    (tier 1), and the two values disagreeing flagged a conflict."""
    facts = [_fact("us-gaap:CommercialPaper", 1000, "K", K_FILED),
             _fact("us-gaap:ShortTermBorrowings", 900, "Q", Q_FILED)]
    positions = [_position("K", "CommercialPaper", K_FILED),
                 _position("Q", "Assets", Q_FILED)]
    found = [r for r in _select(facts, positions)
             if r["concept"] == "short_term_debt" and r["valid_date"] == Q1]
    assert [(r["value"], r["filing_ref"], r["location"], r["conflict"]) for r in found] == [
        (Decimal(900), "Q", "note", False)]


def _debt_lines(filing, known_at):
    facts = [_fact("us-gaap:SeniorNotes", 700, filing, known_at),
             _fact("us-gaap:ConvertibleNotesPayable", 300, filing, known_at)]
    positions = [_position(filing, "SeniorNotes", known_at),
                 _position(filing, "ConvertibleNotesPayable", known_at)]
    return facts, positions


@pytest.mark.parametrize("known_at, expected", [(K_FILED, None), (Q_FILED, Decimal(1000))])
def test_a_sum_of_statement_lines_follows_the_rule(known_at, expected):
    """debt_components sums its balance-sheet lines (on_several = sum): early lines are dropped
    before the sum, on-time ones are summed."""
    rows = _select(*_debt_lines("K", known_at))
    assert _value(rows, "debt_components") == expected


def test_a_dividend_declared_ahead_of_its_quarter_is_kept():
    rows = _select([_fact("us-gaap:CommonStockDividendsPerShareDeclared", "0.49", "K", K_FILED,
                          start=date(2014, 1, 1), frequency="quarterly", unit="USD/shares")])
    assert _value(rows, "dividends_per_share") == Decimal("0.49")


@pytest.mark.parametrize("known_at, kept", [
    (datetime(2014, 3, 31, 2, 0, tzinfo=UTC), False),       # 2014-03-30 22:00 in New York
    (datetime(2014, 3, 31, 23, 59, 59, tzinfo=UTC), True),  # F1's end of the filing day
    (datetime(2014, 4, 1, 3, 0, tzinfo=UTC), True),         # 2014-03-31 23:00 in New York
])
def test_the_filing_day_is_read_in_new_york(known_at, kept):
    rows = _select([_fact("us-gaap:ShortTermBorrowings", 1000, "K", known_at)])
    assert (_value(rows, "short_term_debt") is not None) is kept


def test_the_filing_zone_is_a_parameter():
    rows = _select([_fact("us-gaap:ShortTermBorrowings", 1000, "K",
                          datetime(2014, 3, 31, 2, 0, tzinfo=UTC))], filing_tz="UTC")
    assert _value(rows, "short_term_debt") == Decimal(1000)
    assert DEFAULT_FILING_TZ == "America/New_York"


def test_an_unknown_filing_zone_is_refused():
    with pytest.raises(ValueError, match="filing_tz"):
        selection_sql(filing_tz="Mars/Olympus")


def _read(path):
    return json.loads(resources.files("company_corpus.concepts").joinpath(path)
                      .read_text(encoding="utf-8"))


def _dictionary(top: str, liabilities: str):
    concepts = _read("concepts.json")
    concepts["filed_before_period_end"] = top
    concepts["concepts"]["liabilities"]["filed_before_period_end"] = liabilities
    return parse_dictionary(concepts, [("us_gaap", _read("tags/us_gaap.json"))])


def _all_keep_dictionary():
    """The dictionary as before chantier 11: every concept keeps its early facts."""
    concepts = _read("concepts.json")
    concepts["filed_before_period_end"] = "keep"
    for c in concepts["concepts"].values():
        c.pop("filed_before_period_end", None)
    return parse_dictionary(concepts, [("us_gaap", _read("tags/us_gaap.json"))])


@pytest.mark.parametrize("liabilities, expected", [("keep", Decimal(60)), ("drop", None)])
def test_a_computed_total_follows_its_concept_rule(liabilities, expected):
    """liabilities = LiabilitiesAndStockholdersEquity - equity, both read on the balance sheet.
    Equity keeps its early fact here, so only the computed total's own rule decides."""
    facts = [_fact("us-gaap:LiabilitiesAndStockholdersEquity", 100, "K", K_FILED),
             _fact("us-gaap:StockholdersEquity", 40, "K", K_FILED)]
    positions = [_position("K", "LiabilitiesAndStockholdersEquity", K_FILED),
                 _position("K", "StockholdersEquity", K_FILED)]
    rows = _select(facts, positions, dictionary=_dictionary("keep", liabilities))
    assert _value(rows, "equity") == Decimal(40)
    assert _value(rows, "liabilities") == expected


@pytest.mark.parametrize("name, concept, day, filed_early", [
    ("lilly_early", "short_term_debt", "2014-03-31", "0000059478-14-000078"),
    ("echostar_early", "long_term_debt_incl_current", "2024-03-31", "0001558370-24-002209"),
])
def test_real_balances_filed_before_their_date_leave_the_quarter(name, concept, day, filed_early):
    fx = fixture(name)
    # The early fact is in the input...
    early = [p for p in select_for_f1(fx["facts"], fx["statements"]).points
             if p.end.isoformat() == day and p.accn == filed_early and p.filed < p.end]
    assert early
    # ...and no longer selected: the concept is absent for that date.
    assert pick(select_for_f1(fx["facts"], fx["statements"]).rows, concept, day) is None
    q = {(s.period_end.isoformat(), s.frequency): s
         for s in build_summaries(fx["facts"], company="x", company_current="x",
                                  statements=fx["statements"])}[(day, "quarterly")]
    assert concept not in q.values
    assert q.publication_date > date.fromisoformat(day)   # still dated by its on-time filing


def test_the_rest_of_the_real_quarter_is_unchanged():
    fx = fixture("lilly_early")
    q = {(s.period_end, s.frequency): s for s in build_summaries(
        fx["facts"], company="x", company_current="x", statements=fx["statements"])}
    s = q[(date(2014, 3, 31), "quarterly")]
    assert (s.publication_date, s.accession) == (date(2014, 4, 28), "0000059478-14-000102")
    before = {(b.period_end, b.frequency): b for b in build_summaries(
        fx["facts"], company="x", company_current="x", statements=fx["statements"],
        dictionary=_all_keep_dictionary())}[(date(2014, 3, 31), "quarterly")]
    assert "short_term_debt" in before.values           # what the quarter had before chantier 11
    assert "short_term_debt" not in s.values
    assert set(s.values) == set(before.values) - {"short_term_debt"}
    assert len(s.values) == 40


# --- a period whose only duration rows were early (spec case 6) ---------------------------------

K_DAY, S_DAY = date(2014, 2, 19), date(2014, 8, 5)


def _point(start, end, val, accn, filed, fp, form):
    return {"start": start, "end": end, "val": val, "accn": accn, "fy": 2014, "fp": fp,
            "form": form, "filed": filed}


def _companyfacts():
    """FY2013 revenue on time in the 10-K, which also (wrongly) holds a Q1 2014 revenue: the
    issuer's only quarterly fact, filed before Q1 ended. H1 2014 comes on time."""
    revenue = [_point("2013-01-01", "2013-12-31", 4000, "K", "2014-02-19", "FY", "10-K"),
               _point("2014-01-01", "2014-03-31", 1000, "K", "2014-02-19", "FY", "10-K"),
               _point("2014-01-01", "2014-06-30", 2100, "S", "2014-08-05", "Q2", "10-Q")]
    return {"cik": 1, "entityName": "X",
            "facts": {"us-gaap": {"Revenues": {"units": {"USD": revenue}}}}}


def _early_quarter_rows():
    k_at, s_at = datetime(2014, 2, 19, 21, tzinfo=UTC), datetime(2014, 8, 5, 21, tzinfo=UTC)
    facts = [_fact("us-gaap:Revenues", 4000, "K", k_at, end=date(2013, 12, 31),
                   start=date(2013, 1, 1), frequency="annual"),
             _fact("us-gaap:Revenues", 1000, "K", k_at, end=Q1, start=date(2014, 1, 1),
                   frequency="quarterly"),
             _fact("us-gaap:Revenues", 2100, "S", s_at, end=date(2014, 6, 30),
                   start=date(2014, 1, 1), frequency="semiannual")]
    positions = [_position("K", "Revenues", k_at, statement="IS"),
                 _position("S", "Revenues", s_at, statement="IS")]
    return facts, positions


def test_a_period_with_only_early_rows_has_no_summary_in_f1():
    by = {(s.period_end, s.frequency): s for s in build_summaries(
        _companyfacts(), company="x", company_current="x")}
    assert (Q1, "quarterly") not in by
    assert set(by) == {(date(2013, 12, 31), "annual"), (date(2014, 6, 30), "semi-annual")}
    assert by[(date(2014, 6, 30), "semi-annual")].publication_date == S_DAY
    # Keeping the early fact, the issuer has quarters: no half-year summary, and Q1 none either
    # (only seen before it ended).
    assert [(s.period_end, s.frequency) for s in build_summaries(
        _companyfacts(), company="x", company_current="x",
        dictionary=_all_keep_dictionary())] == [(date(2013, 12, 31), "annual")]


def test_a_period_with_only_early_rows_has_no_summary_in_the_store():
    facts, positions = _early_quarter_rows()
    rows = _select(facts, positions)
    by = {(s.period_end, s.frequency): s
          for s in summaries_from_rows(rows, company="x", vintages=facts)}
    assert (Q1, "quarterly") not in by
    assert set(by) == {(date(2013, 12, 31), "annual"), (date(2014, 6, 30), "semi-annual")}
    assert by[(date(2013, 12, 31), "annual")].publication_date == K_DAY
    kept = _select(facts, positions, dictionary=_all_keep_dictionary())
    assert [(s.period_end, s.frequency)
            for s in summaries_from_rows(kept, company="x", vintages=facts)] == [
        (date(2013, 12, 31), "annual")]


def test_an_issuer_whose_only_quarters_were_early_switches_to_half_years():
    facts, positions = _early_quarter_rows()
    _, _, allowed = group_periods(_select(facts, positions))
    assert allowed == {"annual", "semiannual"}       # quarterly left `seen` with its early rows
    _, _, before = group_periods(_select(facts, positions, dictionary=_all_keep_dictionary()))
    assert before == {"annual", "quarterly"}


# --- known_at must be an instant -----------------------------------------------------------------

def test_a_naive_known_at_is_refused():
    naive = pa.schema([f.with_type(pa.timestamp("us")) if f.name == "known_at" else f
                       for f in FACT_SCHEMA])
    facts = pa.Table.from_pylist([_fact("us-gaap:ShortTermBorrowings", 1000, "K",
                                        datetime(2014, 4, 28, 12))], schema=naive)
    with pytest.raises(ValueError, match="known_at.*TIMESTAMPTZ"):
        run_selection(facts, pa.Table.from_pylist([], schema=POSITION_SCHEMA))


@pytest.mark.parametrize("session_tz", ["Asia/Tokyo", "Pacific/Honolulu", "UTC"])
@pytest.mark.parametrize("known_at, kept", [
    (datetime(2014, 3, 31, 2, 0, tzinfo=UTC), False),
    (datetime(2014, 3, 31, 23, 59, 59, tzinfo=UTC), True),
    (datetime(2014, 4, 1, 3, 0, tzinfo=UTC), True),
])
def test_the_session_time_zone_does_not_move_the_boundary(session_tz, known_at, kept):
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"SET TimeZone = '{session_tz}'")
        con.register("facts", pa.Table.from_pylist(
            [_fact("us-gaap:ShortTermBorrowings", 1000, "K", known_at)], schema=FACT_SCHEMA))
        con.register("positions", pa.Table.from_pylist([], schema=POSITION_SCHEMA))
        rows = con.sql(selection_sql()).to_arrow_table().to_pylist()
    finally:
        con.close()
    assert (_value(rows, "short_term_debt") is not None) is kept
