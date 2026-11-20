"""Every manifest row this corpus writes is a clean row of the vault `documents` contract
(chantier 4): checked with the pinned vault-contract package and the generated vocabulary,
no refusal, and exactly the quality notices expected."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from vault_contract import golden

from company_corpus.config import Config
from company_corpus.models import FilingRecord
from company_corpus.taxonomy import FormType

from .support import VAULT_FORMS, check, reasons, record

FIXTURES = Path(__file__).parent.parent / "fixtures" / "contract"


@pytest.mark.parametrize("form_type", VAULT_FORMS, ids=lambda ft: ft.code)
@pytest.mark.parametrize("transition", [True, False], ids=["transition", "final"])
def test_a_downloaded_rendered_filing_of_every_vault_family_is_clean(form_type, transition):
    checked = check(record(form_type).to_row(), transition=transition)
    assert reasons(checked.refusals) == []
    assert reasons(checked.notices) == []
    assert checked.columns["local_path"].endswith(".pdf")
    assert checked.columns["mime_type"] == "application/pdf"


@pytest.mark.parametrize("form_type", VAULT_FORMS, ids=lambda ft: ft.code)
def test_a_text_only_filing_is_clean(form_type):
    checked = check(record(form_type, pdf_path=None).to_row(), transition=False)
    assert (reasons(checked.refusals), reasons(checked.notices)) == ([], [])
    assert checked.columns["mime_type"] == "text/plain"


def test_text_first_preference_is_clean_too():
    config = Config(vault_artifact_preference=("text", "pdf"))
    row = record(FormType.A1).to_row(artifact_preference=config.vault_artifact_preference)
    checked = check(row, transition=False, config=config)
    assert (reasons(checked.refusals), reasons(checked.notices)) == ([], [])
    assert checked.columns["local_path"].endswith(".txt")


# ---- adversarial records ----

def test_a_filing_with_no_primary_document_and_no_text_is_an_inventory_row():
    rec = record(FormType.B1, primary_path=None, text_path=None, pdf_path=None,
                 primary_sha256=None)
    checked = check(rec.to_row(), transition=False)
    assert (reasons(checked.refusals), reasons(checked.notices)) == ([], [])
    assert (checked.columns["local_path"], checked.columns["mime_type"],
            checked.columns["sha256"]) == (None, None, None)


def test_a_discovered_but_never_downloaded_filing_is_clean():
    rec = record(FormType.C1, submission_path=None, submission_sha256=None, primary_path=None,
                 text_path=None, pdf_path=None, primary_sha256=None)
    checked = check(rec.to_row(), transition=False)
    assert (reasons(checked.refusals), reasons(checked.notices)) == ([], [])


def test_an_undated_filing_states_no_precision_and_is_clean():
    checked = check(record(FormType.D1, filing_date=None).to_row(), transition=False)
    assert (reasons(checked.refusals), reasons(checked.notices)) == ([], [])
    assert (checked.columns["date"], checked.columns["year"],
            checked.columns["date_precision"], checked.columns["date_kind"]) == (
        None, None, None, None)


def test_a_form_4_is_about_its_issuer():
    # Q8: the entity is the company the document is about (Apple), not the insider who filed.
    rec = record(FormType.E1, accession="0001900001-24-000081", sec_form="4")
    checked = check(rec.to_row(), transition=False)
    assert (reasons(checked.refusals), reasons(checked.notices)) == ([], [])
    assert (checked.columns["entity_scheme"], checked.columns["entity_value"]) == (
        "cik", "0000320193")


def test_a_non_edgar_accession_has_no_source_url_and_stays_clean():
    rec = record(FormType.A1, accession="wayback-20240101")
    checked = check(rec.to_row(), transition=False)
    assert (reasons(checked.refusals), reasons(checked.notices)) == ([], [])
    assert checked.columns["source_url"] is None


def test_an_empty_title_is_a_notice_not_a_refusal():
    checked = check(record(FormType.A2, title="").to_row(), transition=False)
    assert reasons(checked.refusals) == []
    assert reasons(checked.notices) == ["empty_title"]


def test_a_pre_contract_row_is_refused_by_the_contract():
    # Why migrate-contract must run before the company ingestion: the old names leave the
    # contract's mandatory source_code and doc_type absent, and a year without a date.
    old = json.loads((FIXTURES / "pre_contract_rows.jsonl").read_text().splitlines()[0])
    checked = check(old)
    assert reasons(checked.refusals) == ["missing_source_code", "missing_doc_type",
                                         "year_mismatch"]
    assert "corpus_missing" in reasons(checked.notices)


def test_an_f1_row_is_refused_by_the_vault_vocabulary():
    # F1 is never a vault document: its doc_type is not in the vocabulary.
    rec = FilingRecord(cik="320193", form_type=FormType.F1, sec_form="10-K/XBRL",
                       accession="0000320193-24-000123", title="Apple Inc. — FY2024",
                       filing_date=date(2024, 11, 1), period_of_report=date(2024, 9, 28),
                       frequency="annual", provenance="edgar_xbrl")
    assert reasons(check(rec.to_row()).refusals) == ["unknown_doc_type", "unknown_provenance"]


# ---- the vault's own golden company rows ----

def _golden(case: str) -> dict:
    return golden.accepted()[case]


def test_the_golden_sec_10k_row_is_exactly_what_to_row_writes():
    row = _golden("sec_10k")["row"]
    assert FilingRecord.from_row(row).to_row() == row


def test_the_golden_form_4_written_by_this_corpus_has_no_notice():
    # The golden Form 4 shows a producer that states neither precision nor mime_type (two
    # notices) and knows its filer; this corpus states both and knows no filer.
    case = _golden("sec_form4")
    assert case["expected_notices"] == ["precision_not_stated", "mime_type_missing"]
    row = FilingRecord.from_row(case["row"]).to_row()
    checked = check(row, transition=False)
    assert (reasons(checked.refusals), reasons(checked.notices)) == ([], [])
    expected = dict(case["expected_columns"], date_precision="day", mime_type="text/plain")
    assert checked.columns == expected
