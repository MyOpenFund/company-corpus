from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from company_corpus.config import Config
from company_corpus.models import FilingRecord
from company_corpus.opendata.fundamentals import fiscal_year_of, fundamentals_rows
from company_corpus.taxonomy import FormType
from tests.opendata.factories import companyfacts, point

CIK = "0000000042"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
K24, K24A, K25, K26 = ("0000000042-24-000001", "0000000042-24-000099",
                       "0000000042-25-000001", "0000000042-26-000001")
ACCEPT = {
    K24: datetime(2024, 7, 30, 20, 6, 22, tzinfo=timezone.utc),
    K24A: datetime(2024, 10, 15, 21, 0, tzinfo=timezone.utc),
    K25: datetime(2025, 7, 30, 20, 0, tzinfo=timezone.utc),
    K26: datetime(2026, 7, 29, 20, 0, tzinfo=timezone.utc),
}


def _rows(us_gaap, *, acceptance=ACCEPT, manifest=(), config=None, now=NOW):
    res = fundamentals_rows(CIK, companyfacts(us_gaap), acceptance=acceptance,
                            manifest_doc_ids=set(manifest), config=config or Config(), now=now)
    return sorted(res.rows, key=lambda r: (r["concept"], r["valid_date"], r["known_at"])), \
        res.counters


def _doc(accn, ft=FormType.A1, form="10-K"):
    return FilingRecord(cik=CIK, form_type=ft, sec_form=form, accession=accn).doc_id


def test_a_row_has_the_contract_columns_and_constants():
    (r,), _ = _rows({"LongTermDebt": {"USD": [point(40152000000, "2024-06-30", accn=K24)]}},
                    manifest=[_doc(K24)])
    assert r == {
        "entity_scheme": "cik", "entity_value": CIK, "concept": "long_term_debt",
        "original_tag": "us-gaap:LongTermDebt", "period_start": None, "frequency": "instant",
        "fiscal_year": None, "basis": "consolidated", "value": Decimal("40152000000.000000"),
        "unit": "USD", "extraction_method": "xbrl_filed", "confidence": None,
        "source_page": None, "valid_date": date(2024, 6, 30), "known_at": ACCEPT[K24],
        "known_at_kind": "published", "source_code": "sec", "source_doc_id": _doc(K24),
    }


def test_a_restatement_is_a_second_row_with_the_amendments_doc_id():
    rows, c = _rows({"Revenues": {"USD": [
        point(100, "2024-06-30", start="2023-07-01", accn=K24),
        point(110, "2024-06-30", start="2023-07-01", accn=K24A, form="10-K/A", filed="2024-10-15"),
    ]}}, manifest=[_doc(K24)])
    assert [(r["value"], r["known_at"], r["source_doc_id"]) for r in rows] == [
        (Decimal(100), ACCEPT[K24], _doc(K24)),
        (Decimal(110), ACCEPT[K24A], _doc(K24A, form="10-K/A")),
    ]
    assert c["filing_not_in_manifest"] == 1   # 10-K/A filings are not crawled (no A1 /A)


def test_comparatives_repeated_in_three_10ks_are_three_rows():
    rows, _ = _rows({"Revenues": {"USD": [
        point(100, "2024-06-30", start="2023-07-01", accn=accn) for accn in (K24, K25, K26)]}})
    assert [r["known_at"] for r in rows] == [ACCEPT[K24], ACCEPT[K25], ACCEPT[K26]]


def test_same_second_filings_equal_values_keep_one_row_lowest_accession():
    same = {K24: ACCEPT[K24], K25: ACCEPT[K24]}
    rows, c = _rows({"Revenues": {"USD": [
        point(100, "2024-06-30", start="2023-07-01", accn=K25),
        point(Decimal("100.0"), "2024-06-30", start="2023-07-01", accn=K24)]}}, acceptance=same)
    assert [r["source_doc_id"] for r in rows] == [_doc(K24)] and c["ambiguous_observation"] == 0


def test_same_second_filings_different_values_emit_nothing():
    same = {K24: ACCEPT[K24], K25: ACCEPT[K24]}
    rows, c = _rows({"Revenues": {"USD": [
        point(100, "2024-06-30", start="2023-07-01", accn=K24),
        point(101, "2024-06-30", start="2023-07-01", accn=K25)]}}, acceptance=same)
    assert rows == [] and c["ambiguous_observation"] == 1


def test_missing_acceptance_falls_back_to_end_of_filing_day_new_york():
    rows, c = _rows({"Revenues": {"USD": [
        point(1, "2024-06-30", start="2023-07-01", accn="0000000042-24-000555",
              filed="2024-07-30")]}})
    assert rows[0]["known_at"] == datetime(2024, 7, 31, 3, 59, 59, tzinfo=timezone.utc)  # EDT
    assert c["known_at_fallback"] == 1


def test_fallback_never_lands_after_the_run():
    now = datetime(2026, 11, 1, 15, tzinfo=timezone.utc)
    rows, _ = _rows({"Revenues": {"USD": [
        point(1, "2026-09-30", start="2026-07-01", accn="0000000042-26-000777",
              filed="2026-11-01", form="10-Q")]}}, now=now)
    assert rows[0]["known_at"] == now


def test_form_outside_the_taxonomy_has_no_doc_id():
    rows, c = _rows({"Revenues": {"USD": [
        point(1, "2024-06-30", start="2023-07-01", accn=K24, form="10-KT")]}})
    assert rows[0]["source_doc_id"] is None and c["source_doc_id_null"] == 1


def test_10q_amendment_maps_to_a2():
    rows, _ = _rows({"Revenues": {"USD": [
        point(1, "2024-03-31", start="2024-01-01", accn=K24, form="10-Q/A")]}})
    assert rows[0]["source_doc_id"] == _doc(K24, FormType.A2, "10-Q/A")


def test_value_is_rounded_to_six_decimals_and_counted():
    rows, c = _rows({"EarningsPerShareDiluted": {"USD/shares": [
        point(1.23456789, "2024-06-30", start="2023-07-01", accn=K24)]}})
    assert rows[0]["value"] == Decimal("1.234568") and c["value_rounded"] == 1


def test_fiscal_year_is_the_year_of_the_next_fiscal_year_end():
    rows, _ = _rows({"Revenues": {"USD": [
        point(400, "2024-06-30", start="2023-07-01", accn=K24),
        point(100, "2023-09-30", start="2023-07-01", accn=K24, form="10-Q"),
        point(110, "2024-09-30", start="2024-07-01", accn=K25, form="10-Q"),  # after last FYE
    ]}})
    assert {(r["valid_date"], r["fiscal_year"]) for r in rows} == {
        (date(2024, 6, 30), 2024), (date(2023, 9, 30), 2024), (date(2024, 9, 30), 2025)}


def test_fiscal_year_change_both_sides():
    # FYE moves from 31 Dec to 31 May: the 5-month 10-KT transition (152 days) is dropped as
    # odd_duration, and its end is a fiscal year-end for the periods around it.
    KT = "0000000042-24-000500"
    rows, c = _rows({"Revenues": {"USD": [
        point(1200, "2023-12-31", start="2023-01-01", accn=K24),
        point(310, "2023-09-30", start="2023-07-01", accn=K24, form="10-Q"),
        point(500, "2024-05-31", start="2024-01-01", accn=KT, form="10-KT"),   # transition
        point(290, "2024-05-31", start="2024-03-01", accn=KT, form="10-KT"),   # its last quarter
        point(1300, "2025-05-31", start="2024-06-01", accn=K25),
        point(300, "2024-08-31", start="2024-06-01", accn=K25, form="10-Q"),
    ]}}, acceptance={**ACCEPT, KT: datetime(2024, 8, 1, 20, tzinfo=timezone.utc)})
    fy = {(r["valid_date"], r["frequency"]): r["fiscal_year"] for r in rows}
    assert fy == {
        (date(2023, 12, 31), "annual"): 2023,
        (date(2023, 9, 30), "quarterly"): 2023,
        (date(2024, 5, 31), "quarterly"): 2024,      # transition quarter
        (date(2025, 5, 31), "annual"): 2025,
        (date(2024, 8, 31), "quarterly"): 2025,
    }
    assert c["dropped:odd_duration"] == 1
    assert c["source_doc_id_null"] == 1                   # 10-KT is outside the taxonomy


@pytest.mark.parametrize("valid, expected", [
    (date(2024, 2, 3), 2024), (date(2025, 2, 1), 2025),   # 53-week drift within slack
    (date(2025, 2, 11), 2026), (date(2023, 5, 1), 2024),
])
def test_fiscal_year_projection_with_slack(valid, expected):
    assert fiscal_year_of(valid, [date(2023, 1, 28), date(2024, 2, 3)], 7) == expected


def test_no_annual_period_means_no_fiscal_year():
    assert fiscal_year_of(date(2024, 3, 31), [], 7) is None


# ---- fix round 1 ----

def test_instant_a_year_before_the_first_year_end_belongs_to_the_prior_year():
    ends = [date(2008, 12, 31), date(2010, 12, 31)]
    assert fiscal_year_of(date(2007, 12, 31), ends, 7) == 2007
    assert fiscal_year_of(date(2006, 12, 31), ends, 7) == 2006
    assert fiscal_year_of(date(2008, 6, 30), ends, 7) == 2008   # inside the first year


def test_52_53_week_instant_a_year_before_the_first_year_end():
    ends = [date(2008, 9, 27), date(2009, 9, 26)]
    assert fiscal_year_of(date(2007, 9, 29), ends, 7) == 2007   # drift within slack
    assert fiscal_year_of(date(2008, 3, 29), ends, 7) == 2008


def test_year_ends_come_only_from_10k_family_annual_points():
    # Non-December filer: the 10-Q reports a trailing-12-months duration ending mid-year.
    rows, _ = _rows({"Revenues": {"USD": [
        point(400, "2024-06-30", start="2023-07-01", accn=K24),
        point(410, "2024-12-31", start="2024-01-01", accn=K25, form="10-Q"),   # 366 days: annual
        point(100, "2024-09-30", start="2024-07-01", accn=K25, form="10-Q"),
    ]}})
    fy = {(r["valid_date"], r["frequency"]): r["fiscal_year"] for r in rows}
    assert fy[(date(2024, 9, 30), "quarterly")] == 2025
    assert fy[(date(2024, 12, 31), "annual")] == 2025


def test_a_10k_amendment_year_end_counts():
    # Only the 10-K/A reports the 2024-12-31 year-end; projection from 2024-06-30 alone would
    # label the 2024-09-30 quarter 2025.
    rows, _ = _rows({"Revenues": {"USD": [
        point(400, "2024-06-30", start="2023-07-01", accn=K24),
        point(100, "2024-09-30", start="2024-07-01", accn=K25, form="10-Q"),
        point(410, "2024-12-31", start="2024-01-01", accn=K26, form="10-K/A"),
    ]}})
    fy = {(r["valid_date"], r["frequency"]): r["fiscal_year"] for r in rows}
    assert fy[(date(2024, 9, 30), "quarterly")] == 2024


# ---- fix round 2 ----

F20, F20B, F40 = "0000000042-25-000020", "0000000042-25-000021", "0000000042-25-000040"


def test_all_20f_issuer_gets_fiscal_years():
    rows, _ = _rows({"Revenues": {"USD": [
        point(400, "2024-03-31", start="2023-04-01", accn=F20, form="20-F"),
        point(300, "2025-03-31", start="2024-04-01", accn=F20B, form="20-F/A"),
    ]}}, acceptance={})
    assert [r["fiscal_year"] for r in rows] == [2024, 2025]


def test_40f_year_ends_count():
    rows, _ = _rows({"Revenues": {"USD": [
        point(400, "2024-12-31", start="2024-01-01", accn=F40, form="40-F")]},
        "LongTermDebt": {"USD": [point(5, "2024-09-30", accn=F40, form="40-F")]}},
        acceptance={})
    assert {r["concept"]: r["fiscal_year"] for r in rows}["long_term_debt"] == 2024


def test_6k_semiannual_and_ttm_points_are_not_year_ends():
    rows, _ = _rows({"Revenues": {"USD": [
        point(400, "2024-03-31", start="2023-04-01", accn=F20, form="20-F"),
        point(150, "2024-09-30", start="2024-04-01", accn=F20B, form="6-K"),   # semiannual
        point(390, "2024-12-31", start="2024-01-01", accn=F40, form="6-K"),    # TTM
    ]}}, acceptance={})
    fy = {r["valid_date"]: r["fiscal_year"] for r in rows}
    assert fy[date(2024, 9, 30)] == 2025 and fy[date(2024, 12, 31)] == 2025


def test_ambiguous_annual_key_still_contributes_its_year_end():
    same = {K24: ACCEPT[K24], K25: ACCEPT[K24]}
    rows, c = _rows({"Revenues": {"USD": [
        point(100, "2024-12-31", start="2024-01-01", accn=K24),
        point(200, "2024-12-31", start="2024-01-01", accn=K25)]},
        "LongTermDebt": {"USD": [point(5, "2024-09-30", accn=K26, form="10-Q")]}},
        acceptance=same | {K26: ACCEPT[K26]})
    assert c["ambiguous_observation"] == 1
    assert [(r["concept"], r["fiscal_year"]) for r in rows] == [("long_term_debt", 2024)]


def test_year_end_hidden_by_a_same_second_10q_still_counts():
    same = {K24: ACCEPT[K24], K25: ACCEPT[K24]}
    rows, _ = _rows({"Revenues": {"USD": [
        point(100, "2024-12-31", start="2024-01-01", accn=K25, form="10-K"),
        point(100, "2024-12-31", start="2024-01-01", accn=K24, form="10-Q")]},
        "LongTermDebt": {"USD": [point(5, "2024-09-30", accn=K26, form="10-Q")]}},
        acceptance=same | {K26: ACCEPT[K26]})
    assert {r["concept"]: r["fiscal_year"] for r in rows}["long_term_debt"] == 2024


def test_annual_forms_config_validation():
    for bad in ((), ("10-K", ""), "10-K", ["10-K"]):
        with pytest.raises(ValueError):
            Config(store_annual_report_forms=bad)


@pytest.mark.parametrize("bad", [-1, 365, 400])
def test_fiscal_year_slack_is_bounded(bad):
    with pytest.raises(ValueError):
        Config(store_fiscal_year_slack_days=bad)


def test_wall_clock_cap_is_counted():
    now = datetime(2026, 11, 1, 15, tzinfo=timezone.utc)
    _, c = _rows({"Revenues": {"USD": [
        point(1, "2026-09-30", start="2026-07-01", accn="0000000042-26-000777",
              filed="2026-11-01", form="10-Q")]}}, now=now)
    assert c["known_at_capped"] == 1


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        _rows({"Revenues": {"USD": [point(1, "2024-06-30", start="2023-07-01", accn=K24)]}},
              now=datetime(2026, 11, 1, 12))


def test_naive_acceptance_is_unusable_and_falls_back():
    rows, c = _rows({"Revenues": {"USD": [
        point(1, "2024-06-30", start="2023-07-01", accn=K24, filed="2024-07-30")]}},
        acceptance={K24: datetime(2024, 7, 30, 20, 6, 22)})
    assert rows[0]["known_at"] == datetime(2024, 7, 31, 3, 59, 59, tzinfo=timezone.utc)
    assert c["known_at_naive"] == 1 and c["known_at_fallback"] == 1


def test_same_second_values_equal_after_quantization_are_not_ambiguous():
    same = {K24: ACCEPT[K24], K25: ACCEPT[K24]}
    rows, c = _rows({"EarningsPerShareDiluted": {"USD/shares": [
        point(Decimal("1.2345671"), "2024-06-30", start="2023-07-01", accn=K24),
        point(Decimal("1.2345674"), "2024-06-30", start="2023-07-01", accn=K25)]}},
        acceptance=same)
    assert len(rows) == 1 and c["ambiguous_observation"] == 0


def test_tie_break_does_not_depend_on_input_order():
    a = point(5, "2024-06-30", start="2023-07-01", accn=K24)
    b = point(5, "2024-06-30", start="2023-07-02", accn=K24)
    out = [_rows({"Revenues": {"USD": pts}})[0] for pts in ([a, b], [b, a])]
    assert out[0] == out[1] and len(out[0]) == 1
