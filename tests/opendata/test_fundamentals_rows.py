from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from company_corpus.config import Config
from company_corpus.models import FilingRecord
from company_corpus.opendata.fundamentals import fundamentals_rows
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
    return sorted(res.rows, key=lambda r: (r["original_tag"], r["valid_date"], r["known_at"])), \
        res.counters


def _doc(accn, ft=FormType.A1, form="10-K"):
    return FilingRecord(cik=CIK, form_type=ft, sec_form=form, accession=accn).doc_id


def test_a_row_has_the_contract_columns_and_constants():
    (r,), _ = _rows({"LongTermDebt": {"USD": [point(40152000000, "2024-06-30", accn=K24)]}},
                    manifest=[_doc(K24)])
    assert r == {
        "entity_scheme": "cik", "entity_value": CIK,
        "original_tag": "us-gaap:LongTermDebt", "period_start": None, "frequency": "instant",
        "fiscal_year": None, "basis": "consolidated", "value": Decimal("40152000000.000000"),
        "unit": "USD", "extraction_method": "xbrl_filed", "confidence": None,
        "source_page": None, "filing_ref": K24, "valid_date": date(2024, 6, 30), "known_at": ACCEPT[K24],
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


def _published(us_gaap, acceptance):
    """{natural key + known_at: row}, as the publisher compares them."""
    rows, _ = _rows(us_gaap, acceptance=acceptance)
    return {(r["entity_value"], r["original_tag"], r["valid_date"], r["frequency"], r["basis"],
             r["known_at"]): r for r in rows}


def _assert_later_filings_change_no_published_row(history, later, acceptance):
    before = _published({"Revenues": {"USD": history}}, acceptance)
    after = _published({"Revenues": {"USD": history + later}}, acceptance)
    assert before and set(before) < set(after)
    for key, row in before.items():
        assert after[key] == row
    assert {r["fiscal_year"] for r in after.values()} == {None}


def test_a_later_10kt_changes_no_published_row_on_a_fiscal_year_change():
    # FYE moves from 30 Jun to 31 Dec. The 10-KT's 184-day period and its last quarter are
    # emitted, and they must not relabel what is already published.
    K, Q, KT = "0000000042-23-000001", "0000000042-23-000002", "0000000042-24-000500"
    acceptance = {K: datetime(2023, 8, 1, tzinfo=timezone.utc),
                  Q: datetime(2023, 11, 1, tzinfo=timezone.utc),
                  KT: datetime(2024, 3, 1, tzinfo=timezone.utc)}
    _assert_later_filings_change_no_published_row(
        [point(100, "2023-06-30", start="2022-07-01", accn=K, filed="2023-08-01"),
         point(30, "2023-09-30", start="2023-07-01", accn=Q, form="10-Q", filed="2023-11-01")],
        [point(60, "2023-12-31", start="2023-07-01", accn=KT, form="10-KT", filed="2024-03-01"),
         point(31, "2023-12-31", start="2023-10-01", accn=KT, form="10-KT", filed="2024-03-01")],
        acceptance)


def test_a_later_53_week_10k_changes_no_published_row():
    # 52/53-week filer: the year ending 3 Jan 2026 must not relabel the September quarter,
    # published before that 10-K existed.
    K23, K24, Q3, K25 = ("0000000042-24-000010", "0000000042-25-000010",
                         "0000000042-25-000020", "0000000042-26-000010")
    acceptance = {K23: datetime(2024, 2, 20, tzinfo=timezone.utc),
                  K24: datetime(2025, 2, 20, tzinfo=timezone.utc),
                  Q3: datetime(2025, 11, 1, tzinfo=timezone.utc),
                  K25: datetime(2026, 2, 20, tzinfo=timezone.utc)}
    _assert_later_filings_change_no_published_row(
        [point(100, "2023-12-30", start="2023-01-01", accn=K23),
         point(100, "2024-12-28", start="2023-12-31", accn=K24),
         point(30, "2025-09-27", start="2025-06-29", accn=Q3, form="10-Q")],
        [point(120, "2026-01-03", start="2024-12-29", accn=K25)],
        acceptance)


def test_a_later_10k_changes_no_published_row():
    K, Q = "0000000042-24-000001", "0000000042-24-000002"
    acceptance = {K: datetime(2024, 8, 1, tzinfo=timezone.utc),
                  Q: datetime(2024, 11, 1, tzinfo=timezone.utc),
                  K25: ACCEPT[K25]}
    _assert_later_filings_change_no_published_row(
        [point(400, "2024-06-30", start="2023-07-01", accn=K),
         point(100, "2024-09-30", start="2024-07-01", accn=Q, form="10-Q")],
        [point(410, "2024-12-31", start="2024-01-01", accn=K25)],
        acceptance)


def test_a_10kt_transition_length_period_is_still_dropped_as_odd_duration():
    # FYE moves from 31 Dec to 31 May: the 5-month transition (152 days) fits no band.
    KT = "0000000042-24-000500"
    rows, c = _rows({"Revenues": {"USD": [
        point(500, "2024-05-31", start="2024-01-01", accn=KT, form="10-KT"),   # transition
        point(290, "2024-05-31", start="2024-03-01", accn=KT, form="10-KT"),   # its last quarter
    ]}}, acceptance={KT: datetime(2024, 8, 1, 20, tzinfo=timezone.utc)})
    assert [(r["valid_date"], r["frequency"], r["value"]) for r in rows] == [
        (date(2024, 5, 31), "quarterly", Decimal(290))]
    assert c["dropped:odd_duration"] == 1
    assert c["source_doc_id_null"] == 1                   # 10-KT is outside the taxonomy


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


def test_a_value_outside_decimal_38_6_is_dropped_and_counted_not_raised():
    rows, counters = _rows({"Revenues": {"USD": [
        point(10 ** 40, "2023-06-30", start="2022-07-01", accn=K24),
        point(100, "2024-06-30", start="2023-07-01", accn=K24),
        # same key, same second: the out-of-range candidate must not crash the ambiguity check
        point(10 ** 40, "2024-06-30", start="2023-07-01", accn=K24A, filed="2024-07-30")]}})
    assert [r["value"] for r in rows] == [Decimal(100)]
    assert counters["dropped:value_out_of_range"] == 2


# --- An implausible filing date without an acceptance time.


def _rows_without_acceptance(filed, now=NOW, config=None):
    return _rows({"Revenues": {"USD": [point(30, "2026-09-30", start="2026-07-01", accn="N",
                                             form="10-Q", filed=filed)]}},
                 acceptance={}, now=now, config=config)


def test_a_filing_date_far_in_the_future_is_dropped_not_re_emitted_every_run():
    rows, c = _rows_without_acceptance("2062-11-20")
    assert rows == [] and c["dropped:implausible_filed"] == 1
    assert c["known_at_capped"] == 0 and c["known_at_fallback"] == 0


def test_the_plausibility_limit_is_the_run_day_in_eastern_time_plus_the_margin():
    # NOW is 2026-11-01 12:00 UTC: Eastern day 2026-11-01, limit 2026-11-08.
    rows, c = _rows_without_acceptance("2026-11-08")
    assert len(rows) == 1 and c["dropped:implausible_filed"] == 0 and c["known_at_capped"] == 1
    rows, c = _rows_without_acceptance("2026-11-09")
    assert rows == [] and c["dropped:implausible_filed"] == 1
    # 02:00 UTC is still the previous Eastern day.
    early = datetime(2026, 11, 2, 2, tzinfo=timezone.utc)
    rows, c = _rows_without_acceptance("2026-11-08", now=early)
    assert len(rows) == 1 and c["dropped:implausible_filed"] == 0


def test_an_accepted_filing_is_never_dropped_for_its_filing_date():
    rows, c = _rows({"Revenues": {"USD": [point(30, "2026-09-30", start="2026-07-01", accn=K26,
                                                form="10-Q", filed="2062-11-20")]}})
    assert len(rows) == 1 and c["dropped:implausible_filed"] == 0


# --- The same-second tie-break is a pure function of the candidates, not of their order.


@pytest.mark.parametrize("reverse", [False, True])
def test_the_tie_break_settles_on_the_form_whatever_the_input_order(reverse):
    pts = [point(5, "2024-06-30", start="2023-07-01", accn=K24, form=f)
           for f in ("10-K", "10-Q")]
    rows, c = _rows({"Revenues": {"USD": list(reversed(pts)) if reverse else pts}})
    assert [r["source_doc_id"] for r in rows] == [_doc(K24)]


@pytest.mark.parametrize("reverse", [False, True])
def test_the_tie_break_settles_on_the_lowest_accession_then_the_unit(reverse):
    # Two filings accepted the same second, one in USD and one in EUR, equal value.
    a, b = "0000000042-24-000002", "0000000042-24-000003"
    accept = {a: ACCEPT[K24], b: ACCEPT[K24]}
    units = {"USD": [point(5, "2024-06-30", start="2023-07-01", accn=b)],
             "EUR": [point(5, "2024-06-30", start="2023-07-01", accn=a)]}
    rows, _ = _rows({"Revenues": dict(reversed(units.items())) if reverse else units},
                    acceptance=accept)
    assert [r["unit"] for r in rows] == ["EUR"]


# --- The anchor tags are the configured ones.


def test_custom_currency_anchor_tags_are_honoured_through_fundamentals_rows():
    us_gaap = {
        "Assets": {"EUR": [point(100, "2024-06-30", accn=K24)]},
        "Revenues": {"USD": [point(1, "2024-06-30", start="2023-07-01", accn=K24)]},
        "NetIncomeLoss": {"USD": [point(2, "2024-06-30", start="2023-07-01", accn=K24)]},
    }
    rows, c = _rows(us_gaap)       # Assets (EUR) contradicts the USD vote: no money
    assert rows == [] and c["dropped:currency_ambiguous"] == 3
    rows, c = _rows(us_gaap, config=Config(store_currency_anchor_tags=("us-gaap:Revenues",)))
    assert {(r["original_tag"], r["unit"]) for r in rows} == {
        ("us-gaap:Revenues", "USD"), ("us-gaap:NetIncomeLoss", "USD")}
    assert c["dropped:currency_ambiguous"] == 0


def test_a_lower_case_report_form_allow_list_is_honoured():
    cfg = Config(store_report_forms=("10-q",))
    rows, c = _rows({"Revenues": {"USD": [
        point(100, "2024-06-30", start="2023-07-01", accn=K24),
        point(30, "2026-09-30", start="2026-07-01", accn=K26, form="10-Q")]}}, config=cfg)
    assert [r["filing_ref"] for r in rows] == [K26] and c["dropped:not_a_report"] == 1
