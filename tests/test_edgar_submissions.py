from __future__ import annotations

from datetime import date

from company_corpus.sources.edgar_submissions import EdgarSubmissions
from company_corpus.taxonomy import FULL_SCOPE, FormType


def test_default_scope_excludes_ownership_and_unmapped(apple_fetcher, config):
    src = EdgarSubmissions(fetcher=apple_fetcher, config=config)
    recs = list(src.discover("320193", scope=FULL_SCOPE))
    forms = sorted(r.form_type.code for r in recs)
    # 10-K (A1), 10-Q (A2), 8-K (B1) kept; Form 4 (E1) and NT 10-K (unmapped) dropped.
    assert forms == ["A1", "A2", "B1"]
    assert not src.errors


def test_url_construction(apple_fetcher, config):
    src = EdgarSubmissions(fetcher=apple_fetcher, config=config)
    tenk = next(r for r in src.discover("320193", scope=FULL_SCOPE) if r.form_type is FormType.A1)
    assert tenk.cik == "0000320193"
    assert tenk.company == "Apple Inc."
    assert tenk.ticker == "AAPL"
    assert tenk.filing_date == date(2024, 11, 1)
    assert tenk.period_of_report == date(2024, 9, 28)
    assert tenk.primary_doc_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"
    )
    assert tenk.submission_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123.txt"
    )
    assert tenk.provenance == "edgar_submissions"


def test_since_filter(apple_fetcher, config):
    src = EdgarSubmissions(fetcher=apple_fetcher, config=config)
    recs = list(src.discover("320193", scope=(FormType.A1, FormType.A2, FormType.B1), since=date(2024, 6, 1)))
    # Only 10-K (2024-11-01) and 10-Q (2024-08-02) survive; 8-K (2024-05-03) drops.
    assert sorted(r.form_type.code for r in recs) == ["A1", "A2"]


def test_missing_cik_records_error(make_fetcher, config):
    src = EdgarSubmissions(fetcher=make_fetcher({}), config=config)
    recs = list(src.discover("999999", scope=FULL_SCOPE))
    assert recs == []
    assert len(src.errors) == 1
    assert src.errors[0]["source"] == "edgar_submissions"


def test_point_in_time_naming(make_fetcher, config):
    subs = {
        "name": "Meta Platforms, Inc.",
        "tickers": ["META"],
        "formerNames": [{"name": "Facebook Inc",
                         "from": "2005-05-06T04:00:00.000Z",
                         "to": "2021-10-27T04:00:00.000Z"}],
        "filings": {"recent": {
            "form": ["10-K", "10-K"],
            "accessionNumber": ["a-2015", "a-2023"],
            "filingDate": ["2015-01-29", "2023-02-02"],
            "reportDate": ["2014-12-31", "2022-12-31"],
            "primaryDocument": ["fb-2014.htm", "meta-2022.htm"],
            "primaryDocDescription": ["10-K", "10-K"],
        }, "files": []},
    }
    src = EdgarSubmissions(fetcher=make_fetcher({"CIK0001326801.json": subs}), config=config)
    recs = {r.accession: r for r in src.discover("1326801", scope=(FormType.A1,))}
    # The 2015 filing is attributed to the name in effect then (Facebook),
    # while the current name is preserved separately.
    assert recs["a-2015"].company == "Facebook Inc"
    assert recs["a-2015"].company_current == "Meta Platforms, Inc."
    assert recs["a-2015"].title.startswith("Facebook Inc 10-K")
    assert recs["a-2023"].company == "Meta Platforms, Inc."
