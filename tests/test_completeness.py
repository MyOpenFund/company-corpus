from __future__ import annotations

from datetime import date

from company_corpus.completeness import build_matrix, expected_count, summarize
from company_corpus.models import FilingRecord
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType


def test_expected_counts():
    assert expected_count(FormType.A1) == 1   # 10-K
    assert expected_count(FormType.A2) == 3   # 10-Q
    assert expected_count(FormType.B1) is None  # 8-K event-driven


def _seed(storage: Storage):
    recs = [
        FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K",
                     accession="a-1", company="Apple Inc.", filing_date=date(2024, 11, 1)),
        FilingRecord(cik="320193", form_type=FormType.A2, sec_form="10-Q",
                     accession="a-2", company="Apple Inc.", filing_date=date(2024, 8, 1)),
        FilingRecord(cik="320193", form_type=FormType.B1, sec_form="8-K",
                     accession="a-3", company="Apple Inc.", filing_date=date(2024, 5, 1)),
    ]
    storage.save_records(recs, dry_run=False)


def test_matrix_statuses(config):
    st = Storage(config)
    _seed(st)
    rows = build_matrix(["320193"], [2024], [FormType.A1, FormType.A2, FormType.B1], storage=st)
    by_form = {r["form_type"]: r for r in rows}
    assert by_form["A1"]["status"] == "ok"        # 1 of 1
    assert by_form["A2"]["status"] == "partial"   # 1 of 3
    assert by_form["B1"]["status"] == "ok"        # event-driven, present
    assert by_form["A1"]["company"] == "Apple Inc."


def test_matrix_missing_and_unknown(config):
    st = Storage(config)  # empty manifest
    rows = build_matrix(["320193"], [2024], [FormType.A1, FormType.B1], storage=st)
    by_form = {r["form_type"]: r for r in rows}
    assert by_form["A1"]["status"] == "missing"   # expected 1, have 0
    assert by_form["B1"]["status"] == "unknown"   # event-driven, have 0


def test_summarize(config):
    st = Storage(config)
    _seed(st)
    rows = build_matrix(["320193"], [2024], [FormType.A1, FormType.A2], storage=st)
    tally = summarize(rows)
    assert tally["ok"] == 1 and tally["partial"] == 1


def test_matrix_company_uses_current_name_not_oldest(config):
    # A manifest spans the issuer's history: an old filing under a former name and
    # a recent one under the current name. The matrix label must be the CURRENT
    # name, not whichever record happens to be first (the oldest) in the manifest.
    st = Storage(config)
    st.save_records([
        FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K", accession="old",
                     company="APPLE COMPUTER INC", company_current="Apple Inc.",
                     filing_date=date(1995, 12, 1)),
        FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K", accession="new",
                     company="Apple Inc.", company_current="Apple Inc.",
                     filing_date=date(2024, 11, 1)),
    ], dry_run=False)
    rows = build_matrix(["320193"], [2024], [FormType.A1], storage=st)
    assert rows[0]["company"] == "Apple Inc."
