from __future__ import annotations

from datetime import date

from company_corpus.models import FilingRecord
from company_corpus.rag import iter_items
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType


def _seed(config, *, pdf=True, accession="acc1"):
    st = Storage(config)
    rec = FilingRecord(
        cik="320193", form_type=FormType.A1, sec_form="10-K", accession=accession,
        company="Apple Inc.", company_current="Apple Inc.", ticker="AAPL",
        filing_date=date(2024, 11, 1), primary_doc_url="https://sec.gov/x/aapl.htm",
        sha256="abc123", entity_id="",
    )
    dest = st.raw_dir_for(rec)
    dest.mkdir(parents=True, exist_ok=True)
    txt = dest / f"{rec.doc_id}.txt"
    txt.write_text("clean text", encoding="utf-8")
    rec.text_path = str(txt.relative_to(config.data_dir))
    if pdf:
        pdf_file = dest / f"{rec.doc_id}.pdf"
        pdf_file.write_bytes(b"%PDF fake")
        rec.pdf_path = str(pdf_file.relative_to(config.data_dir))
    st.save_records([rec], dry_run=False)
    return st, rec


def test_iter_items_prefers_pdf(config):
    _, rec = _seed(config, pdf=True)
    items = list(iter_items(ciks=["320193"], config=config, prefer="pdf"))
    assert len(items) == 1
    it = items[0]
    assert it.doc_id == rec.doc_id
    assert it.path.suffix == ".pdf"
    assert it.payload["source"] == "company-corpus"
    assert it.payload["doc_type"] == "A1"
    assert it.payload["company"] == "Apple Inc."
    assert it.payload["cik"] == "0000320193"
    assert it.payload["url"] == "https://sec.gov/x/aapl.htm"


def test_pdf_falls_back_to_text_when_absent(config):
    _seed(config, pdf=False)
    items = list(iter_items(ciks=["320193"], config=config, prefer="pdf"))
    assert items[0].path.suffix == ".txt"


def test_prefer_text(config):
    _seed(config, pdf=True)
    items = list(iter_items(ciks=["320193"], config=config, prefer="text"))
    assert items[0].path.suffix == ".txt"


def test_filters(config):
    _seed(config, pdf=True)
    assert list(iter_items(ciks=["320193"], doctypes="B", config=config)) == []
    assert list(iter_items(ciks=["320193"], year_min=2025, config=config)) == []
    assert len(list(iter_items(ciks=["320193"], year_min=2024, year_max=2024, config=config))) == 1


def test_skips_when_no_artifact_on_disk(config):
    st = Storage(config)
    rec = FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K",
                       accession="a2", filing_date=date(2024, 11, 1))
    st.save_records([rec], dry_run=False)  # manifest row, but no files
    assert list(iter_items(ciks=["320193"], config=config)) == []


def test_iterates_all_manifests_when_no_ciks(config):
    _seed(config, pdf=True)
    assert len(list(iter_items(config=config))) == 1
