"""Standalone quarters derived from year-to-date rows (chantier 12, rules 1-5)."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal

from company_corpus.concepts.dictionary import load_dictionary
from company_corpus.concepts.quarters import (
    FORMULA_Q2,
    FORMULA_Q3,
    FORMULA_Q4,
    hidden_semiannual,
    metrics_from_derived,
    with_derived_quarters,
)
from company_corpus.concepts.summaries import _value
from company_corpus.financials import PeriodSummary


def at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(hour=21, tzinfo=timezone.utc)


def row(concept, freq, start, end, value, known_at, filing_ref, **kw):
    out = {"concept": concept, "entity_scheme": "sec_cik", "entity_value": "0001652044",
           "valid_date": date.fromisoformat(end), "frequency": freq, "basis": "as_reported",
           "source_code": "sec", "period_start": date.fromisoformat(start) if start else None,
           "value": Decimal(value), "unit": "USD", "original_tag": "us-gaap:CapexTag",
           "filing_ref": filing_ref, "source_doc_id": f"doc-{filing_ref}",
           "known_at": at(known_at), "location": "CF", "conflict": False,
           "review_status": "reviewed", "on_statement": True}
    out.update(kw)
    return out


def ytd_year(concept="capex", **kw):
    return [row(concept, "quarterly", "2024-01-01", "2024-03-31", 12012, "2024-04-25", "Q1", **kw),
            row(concept, "semiannual", "2024-01-01", "2024-06-30", 25198, "2024-07-24", "Q2", **kw),
            row(concept, "ytd9", "2024-01-01", "2024-09-30", 38259, "2024-10-30", "Q3", **kw),
            row(concept, "annual", "2024-01-01", "2024-12-31", 52535, "2025-02-05", "FY", **kw)]


def derived(rows):
    return [r for r in rows if r.get("derived") is not None]


def by_end(rows):
    return {r["valid_date"].isoformat(): r for r in derived(rows)}


def test_ytd_filer_gets_q2_q3_q4():
    rows = ytd_year()
    out = with_derived_quarters(rows)
    assert out[:4] == rows
    got = by_end(out)
    assert {k: v["value"] for k, v in got.items()} == {
        "2024-06-30": Decimal(13186), "2024-09-30": Decimal(13061), "2024-12-31": Decimal(14276)}
    assert [got[k]["derived"] for k in ("2024-06-30", "2024-09-30", "2024-12-31")] == [
        FORMULA_Q2, FORMULA_Q3, FORMULA_Q4]
    assert (FORMULA_Q2, FORMULA_Q3, FORMULA_Q4) == ("6M - Q1", "9M - 6M", "FY - 9M")
    for r in got.values():
        assert r["frequency"] == "quarterly"
        assert r["location"] == "derived"
        assert r["original_tag"] == "derived:us-gaap:CapexTag"
        assert r["concept"] == "capex" and r["unit"] == "USD"
    assert [got[k]["period_start"] for k in ("2024-06-30", "2024-09-30", "2024-12-31")] == [
        date(2024, 4, 1), date(2024, 7, 1), date(2024, 10, 1)]
    assert [r["valid_date"] for r in derived(out)] == [
        date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31)]


def test_known_at_is_the_later_inputs():
    rows = ytd_year()
    q2 = by_end(with_derived_quarters(rows))["2024-06-30"]
    six = rows[1]
    assert (q2["known_at"], q2["filing_ref"], q2["source_doc_id"]) == (
        six["known_at"], six["filing_ref"], six["source_doc_id"])
    q4 = by_end(with_derived_quarters(rows))["2024-12-31"]
    assert q4["filing_ref"] == "FY"


def test_filed_quarter_wins():
    filed = row("capex", "quarterly", "2024-04-01", "2024-06-30", 99999, "2024-07-24", "Q2")
    rows = ytd_year()[:2] + [filed]
    snapshot = dict(filed)
    out = with_derived_quarters(rows)
    assert derived(out) == []
    assert out == rows and out[2] == snapshot


def test_missing_input_gives_nothing():
    rows = ytd_year()[1:3]  # no Q1, no FY
    got = by_end(with_derived_quarters(rows))
    assert list(got) == ["2024-09-30"]
    assert got["2024-09-30"]["value"] == Decimal(13061)
    assert got["2024-09-30"]["derived"] == FORMULA_Q3


def test_non_money_and_instant_excluded():
    eps = ytd_year("eps_diluted", unit="USD/shares")
    instant = [dict(r, period_start=r["period_start"]) for r in ytd_year("cash")]
    out = with_derived_quarters(eps + instant)
    assert derived(out) == []


def test_negative_difference_kept():
    rows = [row("capex", "quarterly", "2024-01-01", "2024-03-31", 500, "2024-04-25", "Q1"),
            row("capex", "semiannual", "2024-01-01", "2024-06-30", 300, "2024-07-24", "Q2")]
    (q2,) = derived(with_derived_quarters(rows))
    assert q2["value"] == Decimal(-200)


def test_not_a_quarter_skipped():
    counters = Counter()
    # Same start, but the year closes five months after the 9M end: the span is no quarter.
    rows = [row("capex", "ytd9", "2024-01-01", "2024-09-30", 38259, "2024-10-30", "Q3"),
            row("capex", "annual", "2024-01-01", "2025-01-31", 60000, "2025-03-20", "FY")]
    assert derived(with_derived_quarters(rows, counters=counters)) == []
    assert counters["quarters:not_a_quarter"] == 1
    # A 9M whose start differs from the FY's never pairs and counts nothing.
    counters = Counter()
    rows = [row("capex", "ytd9", "2023-11-01", "2024-07-31", 30000, "2024-08-30", "Q3"),
            row("capex", "annual", "2024-01-01", "2024-12-31", 52535, "2025-02-05", "FY")]
    assert derived(with_derived_quarters(rows, counters=counters)) == []
    assert counters["quarters:not_a_quarter"] == 0


def test_off_calendar_fiscal_year():
    rows = [row("capex", "quarterly", "2024-06-01", "2024-08-31", 2000, "2024-09-10", "Q1"),
            row("capex", "semiannual", "2024-06-01", "2024-11-30", 4500, "2024-12-10", "Q2")]
    (q2,) = derived(with_derived_quarters(rows))
    assert q2["valid_date"] == date(2024, 11, 30)
    assert q2["period_start"] == date(2024, 9, 1)
    assert q2["value"] == Decimal(2500)


def test_known_at_is_the_later_of_both_inputs():
    """Ruling A3-1: a 9M first seen after the FY (a new filer's comparatives) makes the derived
    Q4 known with the 9M, not with the FY."""
    fy = row("capex", "annual", "2024-01-01", "2024-12-31", 400, "2025-03-01", "FY")
    nine = row("capex", "ytd9", "2024-01-01", "2024-09-30", 290, "2025-11-01", "Q3-25")
    (q4,) = derived(with_derived_quarters([fy, nine]))
    assert (q4["known_at"], q4["filing_ref"], q4["source_doc_id"]) == (
        nine["known_at"], "Q3-25", nine["source_doc_id"])
    assert q4["original_tag"] == "derived:" + fy["original_tag"] and q4["value"] == 110


def test_restated_six_month_used():
    restated = row("capex", "semiannual", "2024-01-01", "2024-06-30", 26000, "2025-07-23", "Q2-25")
    rows = [ytd_year()[0], restated]
    (q2,) = derived(with_derived_quarters(rows))
    assert q2["value"] == Decimal(26000 - 12012)
    assert q2["known_at"] == restated["known_at"] and q2["filing_ref"] == "Q2-25"


def test_idempotent():
    rows = ytd_year() + ytd_year("revenue")
    once = with_derived_quarters(rows)
    assert with_derived_quarters(once) == once
    assert len(derived(once)) == 6


def test_standalone_filer_unchanged():
    rows = [row("capex", "quarterly", "2024-01-01", "2024-03-31", 100, "2024-04-25", "Q1"),
            row("capex", "quarterly", "2024-04-01", "2024-06-30", 110, "2024-07-24", "Q2"),
            row("capex", "quarterly", "2024-07-01", "2024-09-30", 120, "2024-10-30", "Q3"),
            row("capex", "semiannual", "2024-01-01", "2024-06-30", 210, "2024-07-24", "Q2"),
            row("capex", "ytd9", "2024-01-01", "2024-09-30", 330, "2024-10-30", "Q3"),
            row("capex", "annual", "2024-01-01", "2024-12-31", 470, "2025-02-05", "FY")]
    snapshot = [dict(r) for r in rows]
    out = with_derived_quarters(rows)
    assert out[:6] == snapshot
    (q4,) = derived(out)
    assert (q4["valid_date"], q4["value"], q4["derived"]) == (date(2024, 12, 31), Decimal(140),
                                                             FORMULA_Q4)


def _chain_rows(**derived_kw):
    q2 = row("capex", "quarterly", "2024-04-01", "2024-06-30", 13186, "2024-07-24", "Q2",
             location="derived", original_tag="derived:us-gaap:CapexTag", **derived_kw)
    six = row("capex", "semiannual", "2024-04-01", "2024-09-30", 26247, "2024-10-30", "Q3")
    return [q2, six]


def test_never_chained_from_a_derived_row():
    rows = _chain_rows(derived=FORMULA_Q2)
    assert with_derived_quarters(rows) == rows


def test_never_chained_when_the_derived_key_was_dropped():
    rows = _chain_rows()  # a projection lost the `derived` key: the tag still says derived
    assert "derived" not in rows[0]
    assert with_derived_quarters(rows) == rows


def test_derived_total_is_a_valid_input():
    rows = [dict(r, location="derived", original_tag="computed:us-gaap:CapexTag")
            for r in ytd_year()]
    got = by_end(with_derived_quarters(rows))
    assert got["2024-06-30"]["value"] == Decimal(13186)
    assert got["2024-06-30"]["original_tag"] == "derived:computed:us-gaap:CapexTag"


def test_unit_or_basis_mismatch_does_not_pair():
    q1, six = ytd_year()[:2]
    assert derived(with_derived_quarters([q1, dict(six, unit="EUR")])) == []
    assert derived(with_derived_quarters([q1, dict(six, basis="restated")])) == []


def test_two_bases_side_by_side():
    rows = ytd_year() + ytd_year(basis="restated")
    out = derived(with_derived_quarters(rows))
    assert len(out) == 6
    for basis in ("as_reported", "restated"):
        assert [r["value"] for r in out if r["basis"] == basis] == [
            Decimal(13186), Decimal(13061), Decimal(14276)]


def test_conflict_is_either_input_on_statement_is_both():
    q1, six = ytd_year()[:2]
    (q2,) = derived(with_derived_quarters([dict(q1, conflict=True), six]))
    assert q2["conflict"] is True and q2["on_statement"] is True
    (q2,) = derived(with_derived_quarters([q1, dict(six, on_statement=False)]))
    assert q2["conflict"] is False and q2["on_statement"] is False
    (q2,) = derived(with_derived_quarters([dict(q1, on_statement=False), six]))
    assert q2["on_statement"] is False


def test_duplicate_six_month_gives_one_quarter():
    q1, six = ytd_year()[:2]
    (q2,) = derived(with_derived_quarters([q1, six, dict(six)]))
    assert q2["value"] == Decimal(13186)


def test_filed_quarter_checked_before_the_band():
    counters = Counter()
    rows = [row("capex", "ytd9", "2024-01-01", "2024-09-30", 38259, "2024-10-30", "Q3"),
            row("capex", "annual", "2024-01-01", "2025-01-31", 60000, "2025-03-20", "FY"),
            row("capex", "quarterly", "2024-11-01", "2025-01-31", 21000, "2025-03-20", "FY")]
    assert with_derived_quarters(rows, counters=counters) == rows
    assert counters["quarters:not_a_quarter"] == 0


# --- Task A2: derived markers (spec rules 6, 8, 10) ---------------------------------------------

def test_value_carries_derived_formula():
    d = load_dictionary()
    out = with_derived_quarters(ytd_year())
    q2 = by_end(out)["2024-06-30"]
    assert _value(q2, d, ("us-gaap",))["derived"] == "6M - Q1"
    assert _value(out[0], d, ("us-gaap",))["derived"] is None


def test_value_tag_of_a_derived_row_is_the_filed_tag():
    d = load_dictionary()
    out = with_derived_quarters(ytd_year("net_income", original_tag="us-gaap:ProfitLoss"))
    assert {_value(r, d, ("us-gaap",))["tag"] for r in out} == {"ProfitLoss"}


def test_derived_net_income_keeps_the_mixed_equity_base_gate():
    d = load_dictionary()
    out = with_derived_quarters(ytd_year("net_income", original_tag="us-gaap:ProfitLoss"))
    filed_ni, derived_ni = (_value(r, d, ("us-gaap",)) for r in (out[0], by_end(out)["2024-06-30"]))
    base = {"equity": _tv(1000, "StockholdersEquity"),
            "noncontrolling_interest": _tv(300, "MinorityInterest"),
            "shares_outstanding": _tv(10, "CommonStockSharesOutstanding")}
    ungated = _summary({**base, "net_income": _tv(100, "NetIncomeLoss")}).derived
    assert "book_value_per_share" in ungated  # the fixture does reach the gate
    for ni in (filed_ni, derived_ni):
        got = _summary({**base, "net_income": ni}).derived
        assert "book_value_per_share" not in got
        assert "tangible_book_value_per_share" not in got


def test_hidden_semiannual_only_where_a_quarter_ends():
    out = with_derived_quarters(ytd_year())
    assert hidden_semiannual(out) == {date(2024, 6, 30)}
    # a quarter of another concept hides the 6-month row too
    other_concept_q = [row("revenue", "quarterly", "2024-04-01", "2024-06-30", 5, "2024-07-24",
                           "Q2")]
    assert hidden_semiannual(ytd_year()[1:2] + other_concept_q) == {date(2024, 6, 30)}
    # semiannual-only issuer: keeps its 6-month rows
    semi = [row("capex", "semiannual", "2023-01-01", "2023-06-30", 10, "2023-08-01", "H1"),
            row("capex", "annual", "2023-01-01", "2023-12-31", 30, "2024-03-01", "FY")]
    assert hidden_semiannual(with_derived_quarters(semi)) == set()
    # another issuer's quarter does not hide this issuer's 6-month row
    foreign = [row("capex", "quarterly", "2024-04-01", "2024-06-30", 5, "2024-07-24", "X",
                   entity_value="0000000001")]
    assert hidden_semiannual(ytd_year()[1:2] + foreign) == set()


def test_hidden_semiannual_switcher_keeps_its_semiannual_years():
    rows = [row("capex", "semiannual", "2022-01-01", "2022-06-30", 10, "2022-08-01", "H1"),
            row("capex", "annual", "2022-01-01", "2022-12-31", 30, "2023-03-01", "FY22")]
    rows += ytd_year()
    assert hidden_semiannual(with_derived_quarters(rows)) == {date(2024, 6, 30)}


def _summary(values):
    return PeriodSummary(period_end=date(2024, 6, 30), frequency="quarterly",
                         publication_date=date(2024, 7, 24), sec_form="10-Q", accession="Q2",
                         company="X", company_current="X", values=values)


def _v(value, derived=None):
    return {"value": value, "unit": "USD", "label": "", "tag": "", "location": "CF",
            "conflict": False, "on_statement": True, "derived": derived}


def _tv(value, tag, derived=None):
    return {**_v(value, derived), "tag": tag}


def test_metrics_from_derived():
    mixed = _summary({"cfo": _v(50000), "capex": _v(13186, FORMULA_Q2),
                      "revenue": _v(200000), "net_income": _v(30000)})
    assert "net_margin" in mixed.derived
    assert metrics_from_derived(mixed) == frozenset(
        {"free_cash_flow", "fcf_margin", "capex_intensity", "cash_conversion", "total_payout"})
    filed = _summary({"cfo": _v(50000), "capex": _v(13186)})
    assert metrics_from_derived(filed) == frozenset()
    # values without a "derived" key (older producers) count as filed
    bare = _summary({"cfo": {"value": 50000, "unit": "USD"}, "capex": {"value": 1, "unit": "USD"}})
    assert metrics_from_derived(bare) == frozenset()


def test_metrics_from_derived_marks_a_metric_present_both_ways_but_different():
    s = _summary({"cfo": _v(100), "capex": _v(40), "dividends_paid": _v(10),
                  "buybacks": _v(20, FORMULA_Q2)})
    without = _summary({"cfo": _v(100), "capex": _v(40), "dividends_paid": _v(10)}).derived
    assert "total_payout" in without and without["total_payout"] != s.derived["total_payout"]
    assert metrics_from_derived(s) == frozenset({"total_payout"})


def test_rows_from_base_carries_the_derived_formula():
    """Spec rule 8, §4: a derived reported row never looks filed on the corpus rows path."""
    from company_corpus.financials import rows_from_base

    s = PeriodSummary(period_end=date(2024, 6, 30), frequency="quarterly",
                      publication_date=date(2024, 7, 24), sec_form="10-Q", accession="Q2",
                      company="x", company_current="x",
                      values={"capex": {"value": 13186, "unit": "USD", "label": "Capex",
                                        "tag": "CapexTag", "derived": FORMULA_Q2},
                              "revenue": {"value": 1, "unit": "USD", "label": "Revenue",
                                          "tag": "Revenues", "derived": None},
                              "cfo": {"value": 2, "unit": "USD", "label": "CFO", "tag": "C"}})
    rows = {r["concept"]: r for r in rows_from_base({}, s) if r["kind"] == "reported"}
    assert rows["capex"]["derived"] == "6M - Q1"
    assert rows["revenue"]["derived"] is None and rows["cfo"]["derived"] is None
