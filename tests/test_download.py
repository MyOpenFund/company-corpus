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


# ---- repair of interrupted downloads (Rob-C4, DI-M10) ----

class _Boom:
    """A fetcher that fails the test if the repair path reaches the network."""

    def get_text(self, *_a, **_k):
        raise AssertionError("the repair path must not touch the network")


def test_interrupted_download_is_repaired_without_network(apple_fetcher, config):
    st = Storage(config)
    st.fetch_and_store(_apple_10k(), apple_fetcher, dry_run=False)
    # Simulate the interrupt: bytes on disk, manifest never saved.
    stuck = _apple_10k()
    assert stuck.local_path is None

    res = st.fetch_and_store(stuck, _Boom(), dry_run=False)
    assert res.status == "repaired"
    assert stuck.sha256 and stuck.local_path and stuck.primary_path and stuck.text_path


def test_complete_record_is_skipped_with_no_work(apple_fetcher, config):
    st = Storage(config)
    rec = _apple_10k()
    st.fetch_and_store(rec, apple_fetcher, dry_run=False)
    assert st.fetch_and_store(rec, apple_fetcher, dry_run=False).status == "skipped"


def test_missing_sha256_is_backfilled_from_disk(apple_fetcher, config):
    st = Storage(config)
    rec = _apple_10k()
    st.fetch_and_store(rec, apple_fetcher, dry_run=False)
    rec.sha256 = None
    assert st.fetch_and_store(rec, _Boom(), dry_run=False).status == "repaired"
    assert rec.sha256


def test_a_deleted_derived_artefact_is_re_derived(apple_fetcher, config):
    """Adversarial: the primary is gone but sha256 and the submission survive."""
    st = Storage(config)
    rec = _apple_10k()
    st.fetch_and_store(rec, apple_fetcher, dry_run=False)
    (config.data_dir / rec.primary_path).unlink()

    assert st.fetch_and_store(rec, _Boom(), dry_run=False).status == "repaired"
    assert (config.data_dir / rec.primary_path).exists()


def test_repair_is_announced_but_not_performed_in_a_dry_run(apple_fetcher, config):
    st = Storage(config)
    rec = _apple_10k()
    st.fetch_and_store(rec, apple_fetcher, dry_run=False)
    stuck = _apple_10k()
    assert st.fetch_and_store(stuck, _Boom(), dry_run=True).status == "would-repair"
    assert stuck.sha256 is None and stuck.primary_path is None


def test_a_zero_byte_stored_submission_is_an_error_not_a_silent_adoption(config):
    """Adversarial: a truncated download left an empty file on disk.

    Adopting it would stamp a sha256 of the empty string on the record and --
    the pointers being sticky since DI-C1 -- make the document permanently
    "complete" with no text at all.
    """
    st = Storage(config)
    rec = _apple_10k()
    dest = st.raw_dir_for(rec)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{rec.doc_id}.submission.txt").write_text("")

    res = st.fetch_and_store(rec, _Boom(), dry_run=False)
    assert res.status == "error" and "empty" in res.error
    assert rec.sha256 is None


def test_a_malformed_submission_is_one_error_not_an_aborted_run(make_fetcher, config, monkeypatch):
    monkeypatch.setattr("company_corpus.storage.parse_submission",
                        lambda raw: (_ for _ in ()).throw(ValueError("bad SGML")))
    st = Storage(config)
    res = st.fetch_and_store(_apple_10k(), make_fetcher({SUB_URL: "garbage"}), dry_run=False)
    assert res.status == "error" and "bad SGML" in res.error


def test_a_decomposition_failure_writes_no_half_record(make_fetcher, config, monkeypatch):
    """Adversarial: the parse blows up after the submission bytes landed.

    Nothing derived may be stamped on the record -- a half-written sha256 would
    make the next run believe the document is complete (the pointers no longer
    clear themselves since DI-C1).
    """
    monkeypatch.setattr("company_corpus.storage.parse_submission",
                        lambda raw: (_ for _ in ()).throw(ValueError("bad SGML")))
    st = Storage(config)
    rec = _apple_10k()
    st.fetch_and_store(rec, make_fetcher({SUB_URL: "garbage"}), dry_run=False)
    assert rec.local_path  # the bytes are on disk and pointed at
    assert rec.sha256 is None and rec.primary_path is None and rec.text_path is None
    # And the next pass still sees work to do rather than skipping forever.
    assert st.fetch_and_store(rec, _Boom(), dry_run=False).status == "error"


def test_a_primary_less_submission_is_an_error_not_a_repeating_repair(
    apple_fetcher, config, monkeypatch
):
    """Adversarial: the primary is off disk AND the submission yields no primary.

    Stamping the hash and calling that a "repair" left the dangling pointer in
    place (it is sticky since DI-C1, so clearing it would not help either):
    ``_needs_repair`` fired again on the very next pass, and every nightly run
    reported the same document as ``repaired``, inflating ``docs_new`` forever.
    A document that cannot be re-derived from its own bytes is an error.
    """
    st = Storage(config)
    rec = _apple_10k()
    st.fetch_and_store(rec, apple_fetcher, dry_run=False)
    (config.data_dir / rec.primary_path).unlink()
    monkeypatch.setattr("company_corpus.storage.select_primary", lambda *a, **k: None)
    rec.sha256 = "stale"  # a re-stamp would overwrite this sentinel

    for _ in range(2):  # the second pass must not have "fixed" anything
        res = st.fetch_and_store(rec, _Boom(), dry_run=False)
        assert res.status == "error"
        assert "no primary document" in res.error
        assert "re-download with --overwrite" in res.error
        assert rec.sha256 == "stale"


def test_download_universe_counts_a_repair(apple_fetcher, config):
    st = Storage(config)
    st.save_records([_apple_10k()], dry_run=False)
    download_universe(["320193"], scope=FULL_SCOPE, dry_run=False,
                      config=config, fetcher=apple_fetcher, storage=st)
    # Wipe the derived pointers the way an interrupt would have left them.
    manifest = st.load_manifest("320193")
    for rec in manifest.values():
        rec.sha256 = None
    st._write_manifest("320193", manifest.values())

    report = download_universe(["320193"], scope=FULL_SCOPE, dry_run=False,
                               config=config, fetcher=_Boom(), storage=st)
    assert report.repaired == 1 and report.downloaded == 0 and report.errors == 0
    assert next(iter(st.load_manifest("320193").values())).sha256


# ---- --limit caps NEW DOWNLOADS, never repairs ----
def _new_record(accession: str, day: int) -> FilingRecord:
    return FilingRecord(
        cik="320193", form_type=FormType.A1, sec_form="10-K",
        accession=accession, company="Apple Inc.",
        filing_date=date(2024, 11, day),
        primary_doc_url=PRIMARY_URL, submission_url=SUB_URL,
    )


def _half_processed(st: Storage, fetcher, accession: str, day: int) -> FilingRecord:
    """A record whose submission is on disk but whose hash was never stamped.

    Exactly what an interrupt between writing the bytes and saving the manifest
    leaves behind, and what ``fetch_and_store`` repairs from disk with no
    network at all.
    """
    rec = _new_record(accession, day)
    st.fetch_and_store(rec, fetcher, dry_run=False)
    rec.sha256 = None
    return rec


def _mixed_corpus(st: Storage, fetcher) -> None:
    """Three half-processed records and two never-downloaded ones, interleaved.

    The walk is newest-first, so the newest record is a new download: the cap is
    spent on the very first record and everything the run has to converge lies
    behind it.
    """
    recs = [
        _new_record("0000320193-24-000005", 5),                 # new
        _half_processed(st, fetcher, "0000320193-24-000004", 4),
        _new_record("0000320193-24-000003", 3),                 # new
        _half_processed(st, fetcher, "0000320193-24-000002", 2),
        _half_processed(st, fetcher, "0000320193-24-000001", 1),
    ]
    st.save_records(recs, dry_run=False)


def test_download_limit_caps_downloads_but_not_repairs(apple_fetcher, config):
    """``--limit 1`` = one download AND every repair the run walks past.

    The loop used to ``break`` at the cap, so the documented contract ("repairs
    are not capped: they cost no network") was false and every half-processed
    document behind the cap stayed half-processed run after run -- the
    download-free adoption recipe (``--limit 0``) repaired nothing at all.
    """
    st = Storage(config)
    _mixed_corpus(st, apple_fetcher)

    report = download_universe(["320193"], scope=FULL_SCOPE, dry_run=False,
                               limit=1, config=config, fetcher=apple_fetcher,
                               storage=st)

    assert report.downloaded == 1
    assert report.repaired == 3
    assert report.errors == 0
    manifest = st.load_manifest("320193")
    by_acc = {r.accession: r for r in manifest.values()}
    assert all(by_acc[f"0000320193-24-00000{n}"].sha256 for n in (1, 2, 4)), \
        "every half-processed document behind the cap is converged"
    assert not by_acc["0000320193-24-000003"].sha256, \
        "the second NEW download is still capped: it would cost network"


def test_download_limit_zero_is_a_repair_only_run(apple_fetcher, config):
    """``--limit 0``: the download-free convergence pass the README recommends."""
    st = Storage(config)
    _mixed_corpus(st, apple_fetcher)

    report = download_universe(["320193"], scope=FULL_SCOPE, dry_run=False,
                               limit=0, config=config, fetcher=_Boom(), storage=st)

    assert report.downloaded == 0 and report.repaired == 3 and report.errors == 0


def test_download_limit_zero_dry_run_reports_the_repairs(apple_fetcher, config):
    st = Storage(config)
    _mixed_corpus(st, apple_fetcher)

    report = download_universe(["320193"], scope=FULL_SCOPE, dry_run=True,
                               limit=0, config=config, fetcher=_Boom(), storage=st)

    assert report.would_repair == 3 and report.repaired == 0 and report.downloaded == 0
    assert not any(r.sha256 for r in st.load_manifest("320193").values()
                   if r.accession.endswith(("1", "2", "4"))), "a dry run writes nothing"
