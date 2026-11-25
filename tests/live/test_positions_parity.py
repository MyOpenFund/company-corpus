"""Selection parity of live positions against FSDS, on real issuers (recorded, offline).

For every issuer recorded by ``scripts/record_live_fixtures.py`` (``tests/fixtures/live/recorded``),
the same facts go through ``run_selection`` with the positions ``filing_positions`` rebuilds from
the filing's own EDGAR files, and with the FSDS ``pre.txt`` rows of the same accessions (the
corpus's source). ``PARITY_MIN_SHARE`` is the least share of the compared rows on which both give
the same ``(concept, valid_date, frequency, value)``; the rows compared are those the positions
decide (see ``tests/live/recorded.py``). The threshold is Marc's: never lowered without him; a
regression is fixed in ``positions.py``.

Recorded set (2026-11-25, FSDS 2026q1 and 2026q3, report dates from 2022-01-01): Microsoft,
Alphabet, Amazon, Meta, Oracle, Target (January year-end), JPMorgan (bank), Novo Nordisk (IFRS
20-F) and Pitney Bowes (a 10-K/A with full statements, found with ``--find-amendments``).
"""
from collections import Counter

import pytest

from tests.live.recorded import compare, load_inputs, manifest, recorded_ciks

#: Least share of equal ``(concept, valid_date, frequency, value)`` between the selection on live
#: positions and on FSDS positions, per issuer (Marc's parameter).
PARITY_MIN_SHARE = 0.99


@pytest.mark.parametrize("cik", recorded_ciks())
def test_selection_parity_on_recorded_issuers(cik):
    inputs = load_inputs(cik)
    parity = compare(inputs)
    assert parity.compared > 0, f"{cik}: no row decided by positions"
    listed = "\n".join(f"  {d}" for d in parity.differences)
    assert parity.share >= PARITY_MIN_SHARE, (
        f"{parity.name} ({cik}): {parity.equal}/{parity.compared} equal "
        f"({parity.share:.2%} < {PARITY_MIN_SHARE:.0%}):\n{listed}")


@pytest.mark.parametrize("cik", recorded_ciks())
def test_every_recorded_filing_has_positions_on_both_sides(cik):
    """Guards the comparison: a filing without positions on both sides would only borrow."""
    inputs = load_inputs(cik)
    assert not inputs.counters, inputs.counters
    accns = {a["accn"] for a in manifest()["issuers"][cik]["accessions"]}
    assert {r["accn"] for r in inputs.live_positions} == accns
    assert {r["accn"] for r in inputs.fsds_positions} == accns


def test_recorded_set_covers_the_required_cases():
    forms = Counter(a["form"] for e in manifest()["issuers"].values() for a in e["accessions"])
    assert {"10-K", "10-Q", "10-K/A", "20-F"} <= set(forms)
    assert {"0000789019", "0001652044", "0001018724", "0001326801", "0001341439", "0000027419",
            "0000019617"} <= set(recorded_ciks())


def test_positions_match_fsds_on_every_line_fsds_lists():
    """Every FSDS row of a recorded filing is placed by live on the same statement, report and
    version; live lists more (abstracts, axes, members, value-less lines: see ``positions``)."""
    for cik in recorded_ciks():
        inputs = load_inputs(cik)
        live = {(r["accn"], r["statement"], r["tag"]): r for r in inputs.live_positions}
        for r in inputs.fsds_positions:
            got = live.get((r["accn"], r["statement"], r["tag"]))
            assert got is not None, (cik, r)
            assert (got["report"], got["version"], got["period"]) == \
                (r["report"], r["version"], r["period"]), (cik, r, got)
