import hashlib
import json
from datetime import date, datetime, timezone

import pytest

from company_corpus.live.filings import FilingRef, fetch_submissions, list_filings
from company_corpus.opendata.submissions import (
    META_SUFFIX,
    acceptance_times,
    archive_from_bytes,
    filing_blocks,
    load_archive,
)
from company_corpus.sources.edgar_submissions import EXTRA_URL, SUBMISSIONS_URL
from tests.opendata.factories import FETCHED, submissions_main, write_archive

CIK = "0000000042"
PAGE = "CIK0000000042-submissions-001.json"


def _url_of(name: str) -> str:
    return SUBMISSIONS_URL.format(cik=CIK) if name == f"CIK{CIK}.json" else EXTRA_URL.format(name=name)


def _two_file_issuer():
    main = submissions_main(
        filings=[("0000000042-24-000001", "2024-07-30T20:06:22.000Z", "10-K", "2024-07-30", "2024-06-30")],
        files=[PAGE])
    page = {"accessionNumber": ["0000000042-05-000009"],
            "acceptanceDateTime": ["2005-03-01T21:00:00.000Z"], "form": ["10-K"],
            "filingDate": ["2005-03-01"], "reportDate": ["2004-12-31"]}
    return main, page


class FakeSec:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_bytes(self, url, *, max_age_s=None):
        self.calls.append(url)
        return self.routes[url]


def test_archive_from_bytes_matches_load_archive(config):
    main, page = _two_file_issuer()
    on_disk = write_archive(config, CIK, main, pages={PAGE: page})
    d = config.raw_dir / CIK / "submissions"
    main_b = (d / f"CIK{CIK}.json").read_bytes()
    page_b = (d / PAGE).read_bytes()
    loaded = load_archive(config, CIK)
    mem = archive_from_bytes(CIK, main_b, [(PAGE, page_b)], fetched_at=FETCHED, url_of=_url_of)
    assert acceptance_times(mem) == acceptance_times(loaded) == acceptance_times(on_disk)
    assert filing_blocks(mem) == filing_blocks(loaded)
    assert [f.name for f in mem.files] == [f.name for f in loaded.files]
    assert [f.rel_path for f in mem.files] == [_url_of(f"CIK{CIK}.json"), _url_of(PAGE)]
    assert [f.url for f in mem.files] == [f.rel_path for f in mem.files]
    assert mem.files[0].sha256 == hashlib.sha256(main_b).hexdigest()
    assert [f.sha256 for f in mem.files] == [f.sha256 for f in loaded.files]
    assert mem.fetched_at == FETCHED


def test_naive_fetched_at_refused():
    main, _ = _two_file_issuer()
    with pytest.raises(ValueError):
        archive_from_bytes(CIK, json.dumps(main).encode(), [], fetched_at=datetime(2026, 11, 1),
                           url_of=_url_of)


def _filings(rows):
    return submissions_main(filings=rows)


def _issuer_with_four_10k():
    rows = []
    for n, year in enumerate((2022, 2023, 2024, 2025), start=1):
        rows.append((f"0000000042-{year % 100:02d}-00000{n}", f"{year}-07-30T20:06:22.000Z", "10-K",
                     f"{year}-07-30", f"{year}-06-30"))
    rows += [("0000000042-24-000011", "2024-04-25T20:00:00.000Z", "10-Q", "2024-04-25", "2024-03-31"),
             ("0000000042-23-000012", "2023-10-25T20:00:00.000Z", "10-Q", "2023-10-25", "2023-09-30"),
             ("0000000042-24-000013", "2024-05-01T12:00:00.000Z", "8-K", "2024-05-01", "")]
    return archive_from_bytes(CIK, json.dumps(_filings(rows)).encode(), [], fetched_at=FETCHED,
                              url_of=_url_of)


def test_list_filings_forms_and_window():
    refs = list_filings(_issuer_with_four_10k(), forms=["10-K"],
                        period_from=date(2022, 1, 1), period_to=date(2025, 12, 31))
    assert [r.report_date for r in refs] == [date(2025, 6, 30), date(2024, 6, 30),
                                             date(2023, 6, 30), date(2022, 6, 30)]
    for r in refs:
        assert isinstance(r, FilingRef) and r.form == "10-K" and r.cik == CIK
        assert r.accepted.tzinfo is timezone.utc
        assert len(r.doc_id) == 16 and int(r.doc_id, 16) >= 0
        assert r.url == ("https://www.sec.gov/Archives/edgar/data/42/"
                         + r.accession.replace("-", ""))
    narrowed = list_filings(_issuer_with_four_10k(), forms=["10-K"],
                            period_from=date(2023, 1, 1), period_to=date(2024, 12, 31))
    assert [r.report_date.year for r in narrowed] == [2024, 2023]


def test_no_filters_lists_everything_newest_first():
    refs = list_filings(_issuer_with_four_10k(), forms=None, period_from=None, period_to=None)
    assert len(refs) == 7
    keys = [(r.accepted) for r in refs]
    assert keys == sorted(keys, reverse=True)


def test_amendment_matches_base_form():
    rows = [("0000000042-24-000001", "2024-07-30T20:06:22.000Z", "10-K", "2024-07-30", "2024-06-30"),
            ("0000000042-24-000002", "2024-09-30T20:06:22.000Z", "10-K/A", "2024-09-30", "2024-06-30"),
            ("0000000042-24-000003", "2024-10-30T20:06:22.000Z", "10-Q", "2024-10-30", "2024-09-30")]
    a = archive_from_bytes(CIK, json.dumps(_filings(rows)).encode(), [], fetched_at=FETCHED,
                           url_of=_url_of)
    refs = list_filings(a, forms=["10-K"], period_from=None, period_to=None)
    assert [r.form for r in refs] == ["10-K/A", "10-K"]
    assert all(r.doc_id for r in refs)
    assert [r.form for r in list_filings(a, forms=["10-K/A"], period_from=None, period_to=None)] \
        == ["10-K/A"]


def test_missing_acceptance_listed_last_and_dates_none():
    main = _filings([("0000000042-24-000001", "2024-07-30T20:06:22.000Z", "10-K", "2024-07-30", ""),
                     ("0000000042-24-000002", "", "10-Q", "", "")])
    a = archive_from_bytes(CIK, json.dumps(main).encode(), [], fetched_at=FETCHED, url_of=_url_of)
    refs = list_filings(a, forms=None, period_from=None, period_to=None)
    assert [r.accession for r in refs] == ["0000000042-24-000001", "0000000042-24-000002"]
    assert refs[1].accepted is None and refs[1].filed is None and refs[1].report_date is None
    assert list_filings(a, forms=None, period_from=date(2020, 1, 1), period_to=None) == []


def test_overflow_pages_fetched():
    main, page = _two_file_issuer()
    sec = FakeSec({SUBMISSIONS_URL.format(cik=CIK): json.dumps(main).encode(),
                   EXTRA_URL.format(name=PAGE): json.dumps(page).encode()})
    a = fetch_submissions(CIK, sec, max_age_s=60.0, now=FETCHED)
    assert sec.calls == [SUBMISSIONS_URL.format(cik=CIK), EXTRA_URL.format(name=PAGE)]
    assert set(acceptance_times(a)) == {"0000000042-24-000001", "0000000042-05-000009"}
    assert a.fetched_at == FETCHED


def test_fetch_submissions_normalizes_cik_and_refuses_unsafe_page_name():
    main = submissions_main(files=["../evil.json"])
    sec = FakeSec({SUBMISSIONS_URL.format(cik=CIK): json.dumps(main).encode()})
    with pytest.raises(ValueError):
        fetch_submissions("42", sec, max_age_s=None, now=FETCHED)
    assert sec.calls == [SUBMISSIONS_URL.format(cik=CIK)]
