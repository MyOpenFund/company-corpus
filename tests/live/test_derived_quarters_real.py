"""Derived quarters on real issuers (chantier 12, spec §8), on the recorded SEC responses.

Store-shaped rows: the producer's ``fundamentals`` rows of each recorded issuer
(``tests/live/recorded.py``) through ``run_selection`` with its live statement positions, as a
store reader runs it. Alphabet, Meta and Oracle (fiscal year ending 31 May) file their cash-flow
statements year to date; Microsoft and Amazon file standalone quarters and get a derived Q4 only.

Pinned capex values (USD) are the subtraction of the filed year-to-date values of the recorded
companyfacts; each summary is dated and accessioned by the first filing of its later input:

=========  ==========  ===========  ==================================  ======================
issuer     quarter     derived      filed inputs                        first filing (later)
=========  ==========  ===========  ==================================  ======================
Alphabet   2024-06-30  13,186 M     6M 25,198 - Q1 12,012               0001652044-24-000079
Alphabet   2024-09-30  13,061 M     9M 38,259 - 6M 25,198               0001652044-24-000118
Alphabet   2024-12-31  14,276 M     FY 52,535 - 9M 38,259               0001652044-25-000014
Meta       2024-06-30   8,173 M     6M 14,573 - Q1 6,400                0001326801-24-000069
Meta       2024-09-30   8,258 M     9M 22,831 - 6M 14,573               0001326801-24-000081
Meta       2024-12-31  14,425 M     FY 37,256 - 9M 22,831               0001326801-25-000017
Oracle     2024-11-30   3,970 M     6M 6,273 - Q1 2,303                 0000950170-24-134973
Oracle     2025-02-28   5,862 M     9M 12,135 - 6M 6,273                0000950170-25-037143
Oracle     2025-05-31   9,080 M     FY 21,215 - 9M 12,135               0000950170-25-087926
=========  ==========  ===========  ==================================  ======================
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pyarrow as pa
import pytest

from company_corpus.concepts import store_summaries
from company_corpus.concepts.quarters import FORMULA_Q2, FORMULA_Q3, FORMULA_Q4, PAIRS
from company_corpus.concepts.selection import run_selection
from company_corpus.concepts.store_summaries import summaries_from_rows
from company_corpus.concepts.summaries import with_quarters
from tests.live.recorded import STATEMENTS_SCHEMA, load_inputs

GOOGL, META, ORCL, MSFT, AMZN = (
    "0001652044", "0001326801", "0001341439", "0000789019", "0001018724")
#: First day of fiscal year 2022 (Oracle's FY2022 runs June 2021 - May 2022).
FY2022 = {GOOGL: date(2022, 1, 1), META: date(2022, 1, 1), ORCL: date(2021, 6, 1),
          MSFT: date(2021, 7, 1), AMZN: date(2022, 1, 1)}
_CAPEX_TAG = "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"
_M = Decimal(1_000_000)

#: issuer -> [(quarter end, formula, capex in USD millions, first filing of the later input,
#: its publication date)].
PINNED = {
    GOOGL: [(date(2024, 6, 30), FORMULA_Q2, 13186, "0001652044-24-000079", date(2024, 7, 23)),
            (date(2024, 9, 30), FORMULA_Q3, 13061, "0001652044-24-000118", date(2024, 10, 29)),
            (date(2024, 12, 31), FORMULA_Q4, 14276, "0001652044-25-000014", date(2025, 2, 4))],
    META: [(date(2024, 6, 30), FORMULA_Q2, 8173, "0001326801-24-000069", date(2024, 7, 31)),
           (date(2024, 9, 30), FORMULA_Q3, 8258, "0001326801-24-000081", date(2024, 10, 30)),
           (date(2024, 12, 31), FORMULA_Q4, 14425, "0001326801-25-000017", date(2025, 1, 29))],
    ORCL: [(date(2024, 11, 30), FORMULA_Q2, 3970, "0000950170-24-134973", date(2024, 12, 10)),
           (date(2025, 2, 28), FORMULA_Q3, 5862, "0000950170-25-037143", date(2025, 3, 11)),
           (date(2025, 5, 31), FORMULA_Q4, 9080, "0000950170-25-087926", date(2025, 6, 18))],
}


def _select(cik: str, as_of: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """(selection rows, fundamentals vintages) of one recorded issuer, the store's rows filtered
    to what was known at ``as_of`` (facts and positions alike) when given."""
    inputs = load_inputs(cik)
    vintages = inputs.facts.to_pylist()
    positions = inputs.live_statements.to_pylist()
    if as_of is not None:
        vintages = [v for v in vintages if v["known_at"] <= as_of]
        positions = [p for p in positions if p["known_at"] <= as_of]
    rows = run_selection(pa.Table.from_pylist(vintages, schema=inputs.facts.schema),
                         pa.Table.from_pylist(positions, schema=STATEMENTS_SCHEMA))
    return rows, vintages


def _hand_subtraction(q: dict, rows: list[dict]) -> Decimal:
    """The derived row ``q`` recomputed from the selection's filed rows: later - earlier."""
    earlier_f, later_f = next((e, lt) for e, lt, f in PAIRS if f == q["derived"])
    same = ("concept", "entity_scheme", "entity_value", "basis", "source_code", "unit")
    (later,) = [r for r in rows if r["frequency"] == later_f and r["valid_date"] == q["valid_date"]
                and all(r[k] == q[k] for k in same)]
    (earlier,) = [r for r in rows if r["frequency"] == earlier_f
                  and r["valid_date"] == q["period_start"] - timedelta(days=1)
                  and r["period_start"] == later["period_start"]
                  and all(r[k] == q[k] for k in same)]
    assert q["original_tag"] == "derived:" + later["original_tag"]
    return later["value"] - earlier["value"]


@pytest.mark.parametrize("cik", [GOOGL, META, ORCL])
def test_ytd_filers_hand_subtraction(cik):
    rows, vintages = _select(cik)
    derived = [r for r in with_quarters(rows) if r.get("derived")]
    since = [r for r in derived if r["valid_date"] >= FY2022[cik]]
    assert since
    for q in since:  # every concept, not only capex
        assert q["value"] == _hand_subtraction(q, rows), (q["concept"], q["valid_date"])
        assert q["location"] == "derived"

    # Capex Q2, Q3 and Q4 are derived in every fiscal year from FY2022 (Q1 is filed).
    capex = {r["valid_date"]: r["derived"] for r in since if r["concept"] == "capex"}
    years = range(FY2022[cik].year, FY2022[cik].year + 4)
    for formula in (FORMULA_Q2, FORMULA_Q3, FORMULA_Q4):
        assert len([d for d, f in capex.items() if f == formula]) >= len(years), formula

    summaries = {(s.period_end, s.frequency): s
                 for s in summaries_from_rows(rows, company="x", vintages=vintages)}
    for end, formula, millions, accession, published in PINNED[cik]:
        s = summaries[(end, "quarterly")]
        capex_value = s.values["capex"]
        assert (capex_value["value"], capex_value["derived"]) == (millions * _M, formula)
        assert capex_value["tag"] == _CAPEX_TAG.split(":")[1]
        assert (s.accession, s.publication_date) == (accession, published)


def _without_derivation(rows, dictionary=None):
    """main's grouping: the selection rows alone."""
    return list(rows)


@pytest.mark.parametrize("cik", [MSFT, AMZN])
def test_standalone_filers_filed_rows_identical(cik, monkeypatch):
    """Spec §8: Microsoft and Amazon keep every filed row and value as on main; the additions are
    derived rows only, a Q4 (FY - 9M) in every fiscal year from FY2022 (D4)."""
    rows, vintages = _select(cik)
    ext = with_quarters(rows)
    assert ext[:len(rows)] == rows
    added = ext[len(rows):]
    assert all(r["derived"] and r["location"] == "derived" for r in added)
    recent = [r for r in added if r["valid_date"] >= FY2022[cik]]
    assert recent and {r["derived"] for r in recent} == {FORMULA_Q4}
    assert {r["valid_date"].year for r in recent if r["concept"] == "capex"} >= {
        2022, 2023, 2024, 2025}  # the calendar years of the FY2022-FY2025 year-ends

    branch = {(s.period_end, s.frequency): s
              for s in summaries_from_rows(rows, company="x", vintages=vintages)}
    monkeypatch.setattr(store_summaries, "with_quarters", _without_derivation)
    main = {(s.period_end, s.frequency): s
            for s in summaries_from_rows(rows, company="x", vintages=vintages)}
    assert set(main) <= set(branch)
    for key, before in main.items():
        after = branch[key]
        assert (after.publication_date, after.accession) == (
            before.publication_date, before.accession), key
        for k, v in after.values.items():
            if k in before.values:
                assert v == before.values[k], (key, k)  # filed value, derived None
            else:  # before FY2022 Amazon's cash-flow lines also give a few Q2/Q3 (2012, 2015)
                assert v["derived"] is not None, (key, k)
                if key[0] >= FY2022[cik]:
                    assert v["derived"] == FORMULA_Q4, (key, k)
    for key in set(branch) - set(main):  # a new fiscal year-end quarter: derived flows only
        assert key[1] == "quarterly"
        flows = [v for v in branch[key].values.values() if v["derived"] is not None]
        assert flows and all(v["derived"] == FORMULA_Q4 for v in flows), key


#: Alphabet's Q3 2024 10-Q, accepted 2024-10-29 23:33:10 UTC (19:33 New York).
Q3_10Q = "0001652044-24-000118"
Q3_END = date(2024, 9, 30)


@pytest.mark.parametrize("as_of, present", [
    (datetime(2024, 10, 28, 23, 59, 59, tzinfo=timezone.utc), False),
    (datetime(2024, 10, 30, 23, 59, 59, tzinfo=timezone.utc), True),
])
def test_as_of_before_and_after_10q(as_of, present):
    """Spec rule 4: as of the day before the 10-Q's acceptance Alphabet's Q3 2024 does not exist
    (neither its 9M input nor the derived quarter, nor a summary); the day after it does."""
    rows, vintages = _select(GOOGL, as_of)
    ext = with_quarters(rows)
    q3 = [r for r in ext if r["concept"] == "capex" and r["valid_date"] == Q3_END
          and r["frequency"] == "quarterly"]
    summaries = {(s.period_end, s.frequency): s
                 for s in summaries_from_rows(rows, company="x", vintages=vintages)}
    if not present:
        assert not [r for r in rows if r["valid_date"] == Q3_END]
        assert not q3 and (Q3_END, "quarterly") not in summaries
        assert (date(2024, 6, 30), "quarterly") in summaries  # the Q2 before it is there
        return
    (q,) = q3
    assert (q["value"], q["derived"], q["filing_ref"]) == (13061 * _M, FORMULA_Q3, Q3_10Q)
    s = summaries[(Q3_END, "quarterly")]
    assert (s.accession, s.publication_date) == (Q3_10Q, date(2024, 10, 29))
    assert s.values["capex"]["derived"] == FORMULA_Q3
