"""End-to-end: run the REAL producers into a directory, then verify that directory.

Every unit test in ``test_verify.py`` and in the producers' own modules passed
while a corpus built by ``eu-acquire`` reported one ``foreign-row`` per document
and a corpus built by ``ownership --write`` reported one ``orphan-artefact`` per
filing. Both bugs live in the seam between a producer and the checker -- the EU
downloader spelled its LEI the way the OAM did while ``verify`` normalises it,
and the ownership writer repointed ``primary_path`` at a new file without
retiring the one it superseded -- and a seam is invisible to a test that stubs
one of its two sides.

So this module stubs neither: it calls the producers with a fetcher that serves
bytes, and then asserts ``verify(config) == []``. A finding here is a real
finding an operator would get on a real corpus.
"""
from __future__ import annotations

from datetime import date

import pytest

from company_corpus.config import Config
from company_corpus.eu.documents import Document
from company_corpus.eu.download import download_document
from company_corpus.eu.entities import Entity
from company_corpus.eu.financials import build_eu_financials
from company_corpus.models import FilingRecord
from company_corpus.pipeline import process_ownership
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType
from company_corpus.verify import verify

from .test_ownership import FORM4_SUBMISSION, THIRTEENF_SUBMISSION

#: The same LEI as the fixtures elsewhere, spelled the way an OAM feed spells
#: it. GLEIF and most OAMs publish upper case; several national ones do not, and
#: a spec file typed by hand is whatever the operator typed.
LOWER_LEI = "5493001kjtiigc8y1r12"
UPPER_LEI = "5493001KJTIIGC8Y1R12"


class _FileFetcher:
    """Serves any URL as the same small body, the way an OAM download does."""

    def __init__(self, body: bytes = b"<xhtml>annual report</xhtml>"):
        self.body = body

    def download(self, url: str, dest) -> None:
        dest.write_bytes(self.body)


def _eu_doc(lei: str, native_id: str = "AR-2023-42") -> Document:
    return Document(
        native_id=native_id, lei=lei, country="FI", doc_type="annual_report",
        period_end=date(2023, 12, 31), published_ts="2024-03-01T00:00:00Z",
        discovered_ts="2024-03-02", language="fi", source="fin-oam",
        files=[{"name": "report.xhtml", "url": "https://oam.invalid/report.xhtml",
                "kind": "primary"}],
    )


def _seed(storage: Storage, **kw) -> FilingRecord:
    rec = FilingRecord(filing_date=date(2024, 5, 1), **kw)
    storage.save_records([rec], dry_run=False)
    return rec


# ---------------------------------------------------------------------------
# EU acquire -> verify
# ---------------------------------------------------------------------------
def test_eu_download_normalises_the_lei_it_files_under(config):
    """The raw directory and the manifest path are the CANONICAL LEI.

    ``verify`` reads ``manifest/<LEI>/`` through ``normalize_lei``; the
    downloader used to spell that directory with ``safe_filename`` alone, so a
    lower-case LEI in a spec file produced ``manifest/5493001kj…/`` -- an
    ``invalid-identifier`` directory whose every document was then a
    ``foreign-row`` (DI-I7, still half open).
    """
    manifest = download_document(_eu_doc(LOWER_LEI), fetcher=_FileFetcher(),
                                 config=config)

    assert manifest["lei"] == UPPER_LEI, "the manifest body carries the canonical LEI"
    # The SPELLING on disk, not ``exists()``: a case-folding filesystem (macOS,
    # and the SMB shares this corpus is served from) answers True for both.
    assert [p.name for p in config.raw_dir.iterdir()] == [UPPER_LEI]
    assert [p.name for p in (config.data_dir / "manifest").iterdir()] == [UPPER_LEI]
    assert manifest["files"][0]["path"].startswith(f"raw/{UPPER_LEI}/")


def test_eu_download_lowercase_lei_verifies_clean(config):
    download_document(_eu_doc(LOWER_LEI), fetcher=_FileFetcher(), config=config)
    assert verify(config) == []


def test_eu_download_keeps_an_unnormalisable_lei_visible(config):
    """A LEI that is not a LEI still lands somewhere, and verify SAYS so.

    The fallback is the sanitised raw value, not a crash and not a silent
    canonicalisation: ``invalid-identifier`` is the legitimate finding for an
    OAM row whose LEI field is junk.
    """
    download_document(_eu_doc("not-a-lei"), fetcher=_FileFetcher(), config=config)
    kinds = {f.kind for f in verify(config)}
    assert kinds == {"invalid-identifier"}, kinds


# ---------------------------------------------------------------------------
# ownership -> verify
# ---------------------------------------------------------------------------
def test_ownership_run_leaves_no_orphan(make_fetcher, config):
    """``write_ownership_summary`` must not strand the decomposed primary.

    ``fetch_and_store`` writes ``<doc_id>.primary.xml`` and points the record at
    it; the summary then writes ``<doc_id>.primary.html`` and repoints the
    record -- leaving the ``.xml`` on disk with nothing pointing at it. That is
    one orphan per ownership filing, i.e. ``verify`` exits 3 on every corpus
    built by ``ownership --write``, forever.
    """
    st = Storage(config)
    _seed(st, cik="320193", form_type=FormType.E1, sec_form="4", accession="acc-f4",
          company="Apple Inc.", primary_doc_url="https://x/form4.xml",
          submission_url="https://sec/form4sub.txt")
    _seed(st, cik="320193", form_type=FormType.E2, sec_form="13F-HR", accession="acc-13f",
          company="Berkshire", submission_url="https://sec/13fsub.txt")
    fetcher = make_fetcher({"form4sub.txt": FORM4_SUBMISSION,
                            "13fsub.txt": THIRTEENF_SUBMISSION})

    rep = process_ownership(["320193"], dry_run=False, config=config,
                            fetcher=fetcher, storage=st)
    assert rep.parsed_insider == 1 and rep.parsed_13f == 1

    assert verify(config) == []


def test_ownership_rerun_stays_clean(make_fetcher, config):
    """Idempotence: a second pass re-renders the summary and strands nothing."""
    st = Storage(config)
    _seed(st, cik="320193", form_type=FormType.E1, sec_form="4", accession="acc-f4",
          company="Apple Inc.", primary_doc_url="https://x/form4.xml",
          submission_url="https://sec/form4sub.txt")
    fetcher = make_fetcher({"form4sub.txt": FORM4_SUBMISSION})
    for _ in range(2):
        process_ownership(["320193"], dry_run=False, config=config,
                          fetcher=fetcher, storage=st)
    assert verify(config) == []


# ---------------------------------------------------------------------------
# every producer at once
# ---------------------------------------------------------------------------
def test_all_producers_into_one_corpus_verify_clean(
        make_fetcher, config, sample_submission, monkeypatch):
    """SEC download + ownership + EU acquire + EU financials, one data dir."""
    st = Storage(config)

    # (a) a narrative SEC filing, downloaded and decomposed
    rec = FilingRecord(
        cik="320193", form_type=FormType.A1, sec_form="10-K",
        accession="0000320193-24-000123", company="Apple Inc.",
        filing_date=date(2024, 11, 1),
        submission_url="https://sec/0000320193-24-000123.txt",
        primary_doc_url="https://sec/aapl-20240928.htm")
    st.fetch_and_store(rec, make_fetcher(
        {"0000320193-24-000123.txt": sample_submission}))
    st.save_records([rec], dry_run=False)

    # (b) ownership, whose summary supersedes the decomposed primary
    _seed(st, cik="320193", form_type=FormType.E1, sec_form="4", accession="acc-f4",
          company="Apple Inc.", primary_doc_url="https://x/form4.xml",
          submission_url="https://sec/form4sub.txt")
    process_ownership(["320193"], dry_run=False, config=config, storage=st,
                      fetcher=make_fetcher({"form4sub.txt": FORM4_SUBMISSION}))

    # (c) an EU document acquired under a lower-case LEI
    download_document(_eu_doc(LOWER_LEI), fetcher=_FileFetcher(), config=config)

    # (d) the EU financials table for the same issuer
    monkeypatch.setattr(
        "company_corpus.eu.financials.resolve_entities",
        lambda specs, **kw: [Entity(lei=UPPER_LEI, name="X", country="FI")])
    build_eu_financials([{"lei": LOWER_LEI}], fetcher=_EsefFetcher(),
                        config=config, write=True)

    assert verify(config) == []


class _EsefFetcher:
    """The filings.xbrl.org shape: an entity listing, then one report JSON."""

    FILING = {"fxo_id": "1", "country": "FI", "period_end": "2023-12-31",
              "date_added": "2024-03-01 00:00:00", "json_url": "/r/2023.json",
              "package_url": "/r/2023.zip", "report_url": "/r/2023.html"}

    def get_json(self, url: str, **_):
        if "/api/entities/" in url:
            return {"data": [{"id": "1", "attributes": self.FILING}]}
        return {"facts": {"f": {"value": 100, "dimensions": {
            "concept": "ifrs-full:Revenue", "entity": "x", "unit": "iso4217:EUR",
            "period": "2023-01-01T00:00:00/2024-01-01T00:00:00"}}}}
