"""Standalone quarters derived from year-to-date rows (chantier 12, rules 1-5)."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal

from company_corpus.concepts.quarters import (
    FORMULA_Q2,
    FORMULA_Q3,
    FORMULA_Q4,
    with_derived_quarters,
)


def at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(hour=21, tzinfo=UTC)


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
