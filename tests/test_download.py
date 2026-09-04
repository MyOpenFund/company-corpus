from __future__ import annotations

from datetime import date

from company_corpus.models import FilingRecord
from company_corpus.pipeline import download_universe
from company_corpus.storage import Storage
from company_corpus.taxonomy import FULL_SCOPE, FormType

SUB_URL = "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123.txt"
PRIMARY_URL = "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"


def _apple_10k() -> FilingRecord:
    return FilingRecord(
        cik="320193", form_type=FormType.A1, sec_form="10-K",
        accession="0000320193-24-000123", company="Apple Inc.",
        filing_date=date(2024, 11, 1),
        primary_doc_url=PRIMARY_URL, submission_url=SUB_URL,
    )


def test_fetch_and_store_writes_three_artifacts(apple_fetcher, config):
    st = Storage(config)
    rec = _apple_10k()
    res = st.fetch_and_store(rec, apple_fetcher, dry_run=False)
    assert res.status == "downloaded"
    assert rec.local_path and rec.primary_path and rec.text_path and rec.sha256

    full = config.data_dir / rec.local_path
    primary = config.data_dir / rec.primary_path
    text = config.data_dir / rec.text_path
    assert full.exists() and primary.exists() and text.exists()
    # Primary doc is the HTML 10-K; cleaned text has no markup, keeps content.
    assert "<html>" in primary.read_text()
    clean = text.read_text()
    assert "Annual Report" in clean and "Net sales were $391 billion." in clean
    assert "<" not in clean and "var x" not in clean


def test_fetch_and_store_is_idempotent(apple_fetcher, config):
    st = Storage(config)
    rec = _apple_10k()
    st.fetch_and_store(rec, apple_fetcher, dry_run=False)
    res2 = st.fetch_and_store(rec, apple_fetcher, dry_run=False)
    assert res2.status == "skipped"


def test_dry_run_downloads_nothing(apple_fetcher, config):
    st = Storage(config)
    rec = _apple_10k()
    res = st.fetch_and_store(rec, apple_fetcher, dry_run=True)
    assert res.status == "would-download"
    assert not (config.data_dir / "raw").exists()


def test_download_universe_updates_manifest(apple_fetcher, config):
    st = Storage(config)
    st.save_records([_apple_10k()], dry_run=False)  # seed manifest
    report = download_universe(["320193"], scope=FULL_SCOPE, dry_run=False,
                               config=config, fetcher=apple_fetcher, storage=st)
    assert report.downloaded == 1 and report.bytes > 0
    rec = next(iter(st.load_manifest("320193").values()))
    assert rec.text_path and rec.sha256  # persisted back to manifest


def test_download_universe_limit(apple_fetcher, config):
    st = Storage(config)
    st.save_records(
        [_apple_10k(),
         FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K",
                      accession="0000320193-23-000106", company="Apple Inc.",
                      filing_date=date(2023, 11, 1), submission_url=SUB_URL)],
        dry_run=False,
    )
    report = download_universe(["320193"], dry_run=False, limit=1,
                               config=config, fetcher=apple_fetcher, storage=st)
    assert report.downloaded == 1


def test_download_year_filter(apple_fetcher, config):
    st = Storage(config)
    st.save_records([
        _apple_10k(),  # filed 2024-11-01
        FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K",
                     accession="0000320193-23-000106", company="Apple Inc.",
                     filing_date=date(2023, 11, 1), submission_url=SUB_URL),
    ], dry_run=False)
    # Only the 2024 filing is in range.
    report = download_universe(["320193"], year_min=2024, dry_run=False,
                               config=config, fetcher=apple_fetcher, storage=st)
    assert report.downloaded == 1


def test_download_date_window(apple_fetcher, config):
    from datetime import date as _d
    st = Storage(config)
    st.save_records([
        _apple_10k(),  # 2024-11-01
        FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K",
                     accession="0000320193-23-000106", company="Apple Inc.",
                     filing_date=date(2023, 11, 1), submission_url=SUB_URL),
    ], dry_run=False)
    report = download_universe(["320193"], since=_d(2024, 1, 1), until=_d(2024, 12, 31),
                               dry_run=False, config=config, fetcher=apple_fetcher, storage=st)
    assert report.downloaded == 1


def test_download_universe_records_error(make_fetcher, config):
    st = Storage(config)
    st.save_records([_apple_10k()], dry_run=False)
    report = download_universe(["320193"], dry_run=False, config=config,
                               fetcher=make_fetcher({}), storage=st)
    assert report.errors == 1
    assert config.discovery_errors_path.exists()
