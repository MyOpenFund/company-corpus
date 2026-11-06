from datetime import date

from company_corpus.financials import build_period_summaries


def _facts(*points):
    facts = {}
    for tag, unit, start, end, val, accn, form, filed in points:
        row = {"start": start, "end": end, "val": val, "accn": accn, "form": form, "filed": filed}
        facts.setdefault(tag, {"units": {}})["units"].setdefault(unit, []).append(row)
    return {"cik": 1, "entityName": "Test", "facts": {"us-gaap": facts}}


def _summaries(*points):
    found = build_period_summaries(_facts(*points), company="Test", company_current="Test")
    return {(s.period_end, s.frequency): s for s in found}


_REVENUE = ("Revenues", "USD", "2014-01-01", "2014-03-31", 100, "0-Q1", "10-Q", "2014-05-01")
# A dividend declared for the next quarter, in the annual report filed before that quarter ends.
_DIVIDEND = ("CommonStockDividendsPerShareDeclared", "USD/shares", "2014-01-01", "2014-03-31", 1, "0-K", "10-K",
             "2014-02-20")


def test_a_filing_before_the_period_ends_does_not_date_the_period():
    s = _summaries(_REVENUE, _DIVIDEND)[(date(2014, 3, 31), "quarterly")]
    assert (s.publication_date, s.accession, s.sec_form) == (date(2014, 5, 1), "0-Q1", "10-Q")
    assert s.values["dividends_per_share"]["value"] == 1     # the fact stays, only its date no longer counts


def test_a_filing_on_the_day_the_period_ends_counts():
    on_the_day = ("Revenues", "USD", "2014-01-01", "2014-03-31", 100, "0-8", "8-K", "2014-03-31")
    s = _summaries(on_the_day, _REVENUE)[(date(2014, 3, 31), "quarterly")]
    assert (s.publication_date, s.accession) == (date(2014, 3, 31), "0-8")


def test_a_period_only_seen_before_it_ended_is_no_summary():
    assert (date(2014, 3, 31), "quarterly") not in _summaries(_DIVIDEND)
