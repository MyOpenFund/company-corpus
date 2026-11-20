from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from company_corpus.config import DEFAULT_VAULT_ARTIFACT_PREFERENCE
from company_corpus.models import (
    DOC_ID_PERIOD_KEYED_FAMILIES, FilingRecord, artifact_for_vault, index_url,
)
from company_corpus.taxonomy import FormType


def _record(**overrides):
    base = dict(
        cik="320193",
        form_type=FormType.A1,
        sec_form="10-K",
        accession="0000320193-24-000123",
        company="Apple Inc.",
        ticker="AAPL",
        filing_date=date(2024, 11, 1),
        period_of_report=date(2024, 9, 28),
    )
    base.update(overrides)
    return FilingRecord(**base)


def test_cik_is_normalized():
    assert _record().cik == "0000320193"


def test_doc_id_is_stable_and_date_independent():
    a = _record(filing_date=date(2024, 11, 1))
    b = _record(filing_date=date(2024, 11, 2))  # corrected date
    # Same cik|form|accession -> identical id despite different filing_date.
    assert a.doc_id == b.doc_id
    assert len(a.doc_id) == 16


def test_doc_id_changes_with_accession():
    a = _record()
    b = _record(accession="0000320193-23-000106")
    assert a.doc_id != b.doc_id


def test_to_row_and_back_roundtrip():
    rec = _record()
    row = rec.to_row()
    assert row["doc_type"] == "A1"
    assert row["family"] == "A"
    assert row["date"] == "2024-11-01"
    assert row["year"] == 2024
    assert row["doc_id"] == rec.doc_id

    restored = FilingRecord.from_row(row)
    assert restored.cik == rec.cik
    assert restored.form_type is FormType.A1
    assert restored.filing_date == rec.filing_date
    assert restored.doc_id == rec.doc_id


def test_form_type_accepts_code_string():
    rec = _record(form_type="B1")
    assert rec.form_type is FormType.B1


def test_doc_id_ignores_the_period_outside_the_period_keyed_families():
    # Only family F keys on the period; every other family's id must stay
    # date-independent so a corrected period_of_report never renames artefacts.
    a = _record(period_of_report=date(2024, 9, 28))
    b = _record(period_of_report=None)
    assert a.doc_id == b.doc_id


def test_doc_id_keys_on_the_period_for_the_period_keyed_families():
    # Family F pseudo-filings share one accession across a filing's comparatives
    # (DI-C2), so the period is part of their identity.
    common = dict(cik="320193", form_type=FormType.F1, sec_form="10-K/XBRL",
                  accession="0000320193-24-000123")
    assert FormType.F1.family in DOC_ID_PERIOD_KEYED_FAMILIES
    assert (FilingRecord(**common, period_of_report=date(2024, 9, 28)).doc_id
            != FilingRecord(**common, period_of_report=date(2023, 9, 30)).doc_id)


# ---- the documents contract (chantier 4) ----

FIXTURES = Path(__file__).parent / "fixtures" / "contract"
APPLE_DIR = "raw/0000320193/A1/2024/cf0e49e6e176a851"


def _full_record(**overrides):
    """The Apple 10-K FY2024 with every artefact of a downloaded, rendered filing."""
    base = dict(
        title="Apple Inc. 10-K (2024)", company_current="Apple Inc.",
        primary_doc_url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                        "aapl-20240928.htm",
        submission_url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                       "0000320193-24-000123.txt",
        submission_path=f"{APPLE_DIR}.submission.txt", submission_sha256="b" * 64,
        primary_sha256="a" * 64, primary_path=f"{APPLE_DIR}.primary.htm",
        text_path=f"{APPLE_DIR}.txt", pdf_path=f"{APPLE_DIR}.pdf",
    )
    base.update(overrides)
    return _record(**base)


def _old_rows() -> list[dict]:
    """Manifest lines as the pre-contract writer produced them (an A1 and an F1)."""
    return [json.loads(line) for line in
            (FIXTURES / "pre_contract_rows.jsonl").read_text(encoding="utf-8").splitlines()]


def test_to_row_writes_the_contract_columns_for_an_sec_filing():
    row = _full_record().to_row()
    assert {k: row[k] for k in (
        "doc_id", "corpus", "source_code", "doc_type", "title", "date", "year",
        "date_precision", "date_kind", "language", "provenance", "document_url", "source_url",
        "mime_type", "sha256", "local_path", "entity_scheme", "entity_value")} == {
        "doc_id": "cf0e49e6e176a851", "corpus": "company", "source_code": "sec",
        "doc_type": "A1", "title": "Apple Inc. 10-K (2024)", "date": "2024-11-01",
        "year": 2024, "date_precision": "day", "date_kind": "publication", "language": "en",
        "provenance": "edgar_index",
        "document_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                        "aapl-20240928.htm",
        "source_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                      "0000320193-24-000123-index.htm",
        "mime_type": "application/pdf", "sha256": "a" * 64,
        "local_path": f"{APPLE_DIR}.pdf", "entity_scheme": "cik", "entity_value": "0000320193",
    }


def test_to_row_writes_the_extras_of_the_spec_and_no_old_name():
    row = _full_record().to_row()
    assert {k: row[k] for k in (
        "cik", "family", "sec_form", "accession", "period_of_report", "company",
        "company_current", "ticker", "entity_id", "primary_path", "text_path", "pdf_path",
        "submission_path", "submission_sha256", "submission_url", "alt_urls", "frequency")} == {
        "cik": "0000320193", "family": "A", "sec_form": "10-K",
        "accession": "0000320193-24-000123", "period_of_report": "2024-09-28",
        "company": "Apple Inc.", "company_current": "Apple Inc.", "ticker": "AAPL",
        "entity_id": "", "primary_path": f"{APPLE_DIR}.primary.htm",
        "text_path": f"{APPLE_DIR}.txt", "pdf_path": f"{APPLE_DIR}.pdf",
        "submission_path": f"{APPLE_DIR}.submission.txt", "submission_sha256": "b" * 64,
        "submission_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
                          "0000320193-24-000123.txt",
        "alt_urls": [], "frequency": "",
    }
    # The pre-contract names are gone: one name per meaning.
    assert not {"form_type", "filing_date", "primary_doc_url"} & set(row)


def test_contract_row_round_trips_identically():
    row = _full_record().to_row()
    assert FilingRecord.from_row(row).to_row() == row
    assert json.loads(json.dumps(row)) == row


def test_a_pre_contract_row_reads_into_the_new_attributes():
    old = _old_rows()[0]
    rec = FilingRecord.from_row(old)
    assert rec.submission_path == old["local_path"]
    assert rec.submission_sha256 == old["sha256"]
    assert rec.primary_sha256 is None, "the old sha256 hashed the submission, not the primary"
    row = rec.to_row()
    assert row["doc_id"] == old["doc_id"]
    assert row["submission_path"] == old["local_path"]
    assert row["submission_sha256"] == old["sha256"]
    assert row["sha256"] is None
    assert row["local_path"] == old["pdf_path"]
    assert row["document_url"] == old["primary_doc_url"]
    assert (row["doc_type"], row["date"]) == (old["form_type"], old["filing_date"])
    # And the contract row it became reads back to the same record.
    assert FilingRecord.from_row(row).to_row() == row


def test_a_pre_contract_row_without_submission_hashed_its_primary():
    # write_financial_summary (family F) stored the hash of the HTML it wrote, with no
    # submission at all: that hash is the primary's.
    old = _old_rows()[1]
    assert old["local_path"] is None and old["sha256"]
    rec = FilingRecord.from_row(old)
    assert (rec.primary_sha256, rec.submission_sha256, rec.submission_path) == \
        (old["sha256"], None, None)
    assert rec.to_row()["sha256"] == old["sha256"]


def test_doc_id_is_unchanged_by_the_contract_rows():
    forms = [ft for ft in FormType if ft.family != "F"]
    for i in range(50):
        ft = forms[i % len(forms)]
        rec = _record(form_type=ft, sec_form=(ft.edgar_forms or ("x",))[0],
                      accession=f"0000320193-{i % 30:02d}-{i:06d}",
                      filing_date=date(2000 + i % 25, 1 + i % 12, 1 + i % 28))
        basis = f"0000320193|{ft.code}|{rec.accession}"
        expected = hashlib.sha1(basis.encode()).hexdigest()[:16]
        assert rec.doc_id == expected
        assert rec.to_row()["doc_id"] == expected
        assert FilingRecord.from_row(rec.to_row()).doc_id == expected


@pytest.mark.parametrize("preference, path, mime", [
    (("pdf", "text"), f"{APPLE_DIR}.pdf", "application/pdf"),
    (("text", "pdf"), f"{APPLE_DIR}.txt", "text/plain"),
    (("text",), f"{APPLE_DIR}.txt", "text/plain"),
])
def test_the_vault_artefact_follows_the_preference(preference, path, mime):
    row = _full_record().to_row(artifact_preference=preference)
    assert (row["local_path"], row["mime_type"]) == (path, mime)


def test_a_record_without_pdf_falls_back_to_its_text():
    row = _full_record(pdf_path=None).to_row()
    assert (row["local_path"], row["mime_type"]) == (f"{APPLE_DIR}.txt", "text/plain")


def test_a_record_without_any_rag_artefact_has_no_local_path_nor_mime_type():
    rec = _full_record(pdf_path=None, text_path=None, primary_path=None, primary_sha256=None)
    assert artifact_for_vault(rec, DEFAULT_VAULT_ARTIFACT_PREFERENCE) == (None, None)
    row = rec.to_row()
    assert (row["local_path"], row["mime_type"], row["sha256"]) == (None, None, None)
    # Still a row: the vault keeps it as inventory.
    assert row["doc_id"] == "cf0e49e6e176a851" and row["submission_path"]


def test_artefact_existence_is_decided_by_the_pointer_not_by_the_disk(tmp_path):
    # No disk read: the row is the same wherever the process runs.
    rec = _full_record()
    assert artifact_for_vault(rec, ("pdf", "text")) == (f"{APPLE_DIR}.pdf", "application/pdf")


def test_an_undated_record_states_no_precision_nor_kind():
    row = _record(filing_date=None).to_row()
    assert (row["date"], row["year"], row["date_precision"], row["date_kind"]) == \
        (None, None, None, None)


def test_empty_urls_are_written_as_absent():
    row = _record(primary_doc_url="", submission_url="").to_row()
    assert row["document_url"] is None
    assert row["submission_url"] == ""


def test_index_url_is_the_edgar_filing_index_page():
    assert index_url("0000320193", "0000320193-24-000123") == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/"
        "0000320193-24-000123-index.htm")


@pytest.mark.parametrize("accession", ["", "acc-f4-1", "0000320193-24-00012", "../x"])
def test_index_url_needs_an_edgar_accession(accession):
    assert index_url("0000320193", accession) is None
    assert _record(accession=accession).to_row()["source_url"] is None


def test_the_entity_is_the_issuer_of_the_manifest_even_for_a_form_4():
    # Q8: the entity is the company the document is about. A Form 4 in Apple's
    # manifest is about Apple, whoever (an officer) filed it.
    rec = _record(form_type=FormType.E1, sec_form="4", accession="0001900001-24-000081")
    row = rec.to_row()
    assert (row["entity_scheme"], row["entity_value"]) == ("cik", "0000320193")
    assert not {"filer_scheme", "filer_value"} & set(row)
