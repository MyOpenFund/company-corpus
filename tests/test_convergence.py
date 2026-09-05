"""discover -> download -> discover must converge, not destroy (DI-C1, Rob-C4, DI-C3)."""
from __future__ import annotations

from datetime import date

from company_corpus import rag
from company_corpus.models import FilingRecord
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType


class _OneShotFetcher:
    """Serves the submission once; a second call is a test failure."""

    def __init__(self, body: str):
        self.body = body
        self.calls: list[str] = []

    def get_text(self, url: str, **_) -> str:
        self.calls.append(url)
        return self.body


def _discovery_record() -> FilingRecord:
    """What EdgarSubmissions.discover builds: every pointer empty."""
    return FilingRecord(
        cik="320193", form_type=FormType.A1, sec_form="10-K",
        accession="0000320193-24-000123", company="Apple Inc.",
        filing_date=date(2024, 11, 1),
        primary_doc_url="https://example.invalid/aapl-20240928.htm",
        submission_url="https://example.invalid/0000320193-24-000123.txt",
    )


def test_discover_download_discover_keeps_rag_items(config, sample_submission):
    st = Storage(config)
    st.save_records([_discovery_record()], dry_run=False)

    rec = next(iter(st.load_manifest("320193").values()))
    fetcher = _OneShotFetcher(sample_submission)
    assert st.fetch_and_store(rec, fetcher).status == "downloaded"
    st.save_records([rec], dry_run=False)

    st.save_records([_discovery_record()], dry_run=False)  # the second discovery

    kept = next(iter(st.load_manifest("320193").values()))
    assert kept.sha256 and kept.local_path and kept.primary_path and kept.text_path
    assert list(rag.iter_items(["320193"], config=config))
