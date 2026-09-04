from __future__ import annotations

from datetime import date

from company_corpus.sources.edgar_index import EdgarFullIndex
from company_corpus.taxonomy import FULL_SCOPE, FormType

MASTER_IDX = """Description:           Master Index of EDGAR Dissemination Feed
Last Data Received:    March 31, 2024
Comments:              webmaster@sec.gov

CIK|Company Name|Form Type|Date Filed|Filename
320193|Apple Inc.|10-K|2024-11-01|edgar/data/320193/0000320193-24-000123.txt
789019|MICROSOFT CORP|10-Q|2024-01-25|edgar/data/789019/0000789019-24-000010.txt
1318605|Tesla, Inc.|4|2024-02-02|edgar/data/1318605/0001318605-24-000020.txt
1045810|NVIDIA CORP|NT 10-K|2024-03-01|edgar/data/1045810/0001045810-24-000030.txt
"""


def test_parses_data_rows_and_skips_headers(make_fetcher, config):
    src = EdgarFullIndex(fetcher=make_fetcher({"master.idx": MASTER_IDX}), config=config)
    recs = list(src.discover(2024, 1, scope=FULL_SCOPE))
    # 10-K and 10-Q kept; Form 4 (ownership) and NT 10-K (unmapped) dropped.
    assert {r.form_type.code for r in recs} == {"A1", "A2"}
    apple = next(r for r in recs if r.cik == "0000320193")
    assert apple.sec_form == "10-K"
    assert apple.accession == "0000320193-24-000123"
    assert apple.filing_date == date(2024, 11, 1)
    assert apple.submission_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/0000320193-24-000123.txt"
    )
    assert apple.provenance == "edgar_index"


def test_cik_filter(make_fetcher, config):
    src = EdgarFullIndex(fetcher=make_fetcher({"master.idx": MASTER_IDX}), config=config)
    recs = list(src.discover(2024, 1, scope=FULL_SCOPE, ciks={"789019"}))
    assert [r.cik for r in recs] == ["0000789019"]


def test_scope_filter(make_fetcher, config):
    src = EdgarFullIndex(fetcher=make_fetcher({"master.idx": MASTER_IDX}), config=config)
    recs = list(src.discover(2024, 1, scope=(FormType.A2,)))
    assert [r.form_type.code for r in recs] == ["A2"]
