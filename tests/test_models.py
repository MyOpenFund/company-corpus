from __future__ import annotations

from datetime import date

from company_corpus.models import DOC_ID_PERIOD_KEYED_FAMILIES, FilingRecord
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
    assert row["form_type"] == "A1"
    assert row["family"] == "A"
    assert row["filing_date"] == "2024-11-01"
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
