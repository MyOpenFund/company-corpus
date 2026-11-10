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
from company_corpus.concepts.summaries import FACT_SCHEMA, POSITION_SCHEMA

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


def _dictionary(top: str, liabilities: str):
    def read(path):
        return json.loads(resources.files("company_corpus.concepts").joinpath(path)
                          .read_text(encoding="utf-8"))
    concepts = read("concepts.json")
    concepts["filed_before_period_end"] = top
    concepts["concepts"]["liabilities"]["filed_before_period_end"] = liabilities
    return parse_dictionary(concepts, [("us_gaap", read("tags/us_gaap.json"))])


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
