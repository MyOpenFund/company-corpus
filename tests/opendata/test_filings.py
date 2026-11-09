from collections import Counter
from datetime import date, datetime, timezone

from company_corpus.opendata.filings import filings_rows
from tests.opendata.factories import filings_block, submissions_main, write_archive

CIK = "0000000042"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
K, KA, KT, Q = ("0000000042-24-000001", "0000000042-24-000002", "0000000042-24-000003",
                "0000000042-24-000004")


def _rows(config, filings, pages=None, now=NOW):
    archive = write_archive(config, CIK, submissions_main(
        cik=CIK, filings=filings, files=list(pages or {})), pages=pages)
    res = filings_rows(CIK, archive, manifest_doc_ids=set(), config=config, now=now)
    return {r["filing_ref"]: r for r in res.rows}, res.counters


def test_one_row_per_report_filing_with_form_and_report_date(config):
    rows, c = _rows(config, [
        (K, "2024-07-30T20:06:22.000Z", "10-K", "2024-07-30", "2024-06-30"),
        (KA, "2024-10-15T21:00:00.000Z", "10-K/A", "2024-10-15", "2024-06-30"),
        (KT, "2025-03-01T21:00:00.000Z", "10-KT", "2025-03-01", "2024-12-31"),
        ("0000000042-24-000009", "2024-08-01T12:00:00.000Z", "4", "2024-08-01", ""),
    ])
    assert set(rows) == {K, KA, KT}
    r = rows[KT]
    assert (r["form"], r["report_date"], r["valid_date"]) == ("10-KT", date(2024, 12, 31),
                                                              date(2025, 3, 1))
    assert r["known_at"] == datetime(2025, 3, 1, 21, tzinfo=timezone.utc)
    assert (r["entity_scheme"], r["entity_value"], r["source_code"], r["known_at_kind"]) == (
        "cik", CIK, "sec", "published")
    assert rows[KA]["form"] == "10-K/A"
    assert rows[KA]["source_doc_id"] is not None  # family from the form without /A
    assert c["dropped:not_a_report"] == 1


def test_a_missing_report_date_is_null_and_counted(config):
    rows, c = _rows(config, [(K, "2024-07-30T20:06:22.000Z", "40-F", "2024-07-30", "")])
    assert rows[K]["report_date"] is None and c["report_date_missing"] == 1


def test_an_invalid_report_date_is_null_and_counted(config):
    rows, c = _rows(config, [(K, "2024-07-30T20:06:22.000Z", "10-K", "2024-07-30", "2024-13-01")])
    assert rows[K]["report_date"] is None and c["report_date_missing"] == 1


def test_a_bad_filing_date_drops_the_row(config):
    rows, c = _rows(config, [(K, "2024-07-30T20:06:22.000Z", "10-K", "", "2024-06-30"),
                             (Q, "2024-07-30T20:06:22.000Z", "10-Q", "2024-02-30", "")])
    assert rows == {} and c["dropped:bad_filing_date"] == 2


def test_no_acceptance_time_falls_back_and_is_capped_on_the_run_day(config):
    rows, c = _rows(config, [(K, "", "10-K", "2026-11-01", "2026-09-30")])
    assert rows[K]["known_at"] == NOW  # 23:59:59 New York that day is after the run: capped
    assert c["known_at_fallback"] == 1 and c["known_at_capped"] == 1


def test_form_spacing_and_case_are_normalised(config):
    rows, _ = _rows(config, [(K, "2024-07-30T20:06:22.000Z", " 10-k ", "2024-07-30", "2024-06-30")])
    assert rows[K]["form"] == "10-K"


def test_an_accession_on_two_pages_with_equal_fields_gives_one_row(config):
    page = filings_block([(K, "2024-07-30T20:06:22.000Z", "10-K", "2024-07-30", "2024-06-30")])
    rows, c = _rows(config, [(K, "2024-07-30T20:06:22.000Z", "10-K", "2024-07-30", "2024-06-30")],
                    pages={f"CIK{CIK}-submissions-001.json": page})
    assert list(rows) == [K] and c["ambiguous_filing"] == 0


def test_an_accession_on_two_pages_with_different_fields_gives_no_row(config):
    page = filings_block([(K, "2024-07-30T20:06:22.000Z", "10-K", "2024-07-30", "2024-03-31")])
    rows, c = _rows(config, [(K, "2024-07-30T20:06:22.000Z", "10-K", "2024-07-30", "2024-06-30"),
                             (Q, "2024-05-01T20:00:00.000Z", "10-Q", "2024-05-01", "2024-03-31")],
                    pages={f"CIK{CIK}-submissions-001.json": page})
    assert list(rows) == [Q] and c["ambiguous_filing"] == 1


def test_the_rows_are_a_pure_function_of_the_archive(config):
    filings = [(K, "2024-07-30T20:06:22.000Z", "10-K", "2024-07-30", "2024-06-30")]
    a, _ = _rows(config, filings)
    b, _ = _rows(config, filings, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert a == b
