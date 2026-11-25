"""Parity report of live statement positions against FSDS, on the recorded issuers (offline).

Usage::

    python scripts/live_parity_report.py [--ciks 789019 ...]

Reads ``tests/fixtures/live/recorded`` (written by ``scripts/record_live_fixtures.py``) and prints,
per issuer, the selection parity that ``tests/live/test_positions_parity.py`` asserts: concept
values equal / different / borrowed (equal values on rows whose filing borrowed the positions of an
earlier one), equal values placed differently (``location`` or ``on_statement``), then each
difference (concept, period, live tag vs FSDS tag, cause guess). A second table compares the
positions themselves: rows each side lists, lines FSDS does not list (by kind), and how live
``line`` numbers relate to FSDS's.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from company_corpus.config import normalize_cik  # noqa: E402
from tests.live.recorded import compare, load_inputs, recorded_ciks  # noqa: E402

#: Element-name suffixes of presentation lines that never carry a fact.
STRUCTURAL = ("Abstract", "Table", "Axis", "Member", "Domain", "LineItems")


def _kind(tag: str) -> str:
    return next((s for s in STRUCTURAL if tag.endswith(s)), "line item")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--ciks", nargs="+")
    args = p.parse_args(argv)
    ciks = [normalize_cik(c) for c in args.ciks] if args.ciks else recorded_ciks()
    print("| Issuer | CIK | compared | equal | different | borrowed | placed differently | share |")
    print("|---|---|---|---|---|---|---|---|")
    diffs, positions = [], []
    for cik in ciks:
        inputs = load_inputs(cik)
        r = compare(inputs)
        print(f"| {r.name} | {cik} | {r.compared} | {r.equal} | {len(r.differences)} | "
              f"{r.borrowed} | {r.placed_differently} | {r.share:.2%} |")
        diffs.extend((r.name, d) for d in r.differences)
        live = {(x["accn"], x["statement"], x["tag"]): x for x in inputs.live_positions}
        fsds = {(x["accn"], x["statement"], x["tag"]): x for x in inputs.fsds_positions}
        only = Counter(_kind(k[2]) for k in set(live) - set(fsds))
        offsets = Counter(live[k]["line"] - fsds[k]["line"] for k in set(live) & set(fsds))
        same_report = sum(live[k]["report"] == fsds[k]["report"] for k in set(live) & set(fsds))
        positions.append((r.name, len(live), len(fsds), len(set(fsds) - set(live)), only,
                          same_report, offsets))
    print(f"\nDifferences: {len(diffs)}")
    for name, d in diffs:
        print(f"- {name}: {d}")
    print("\n| Issuer | live rows | FSDS rows | FSDS only | live only (by kind) | same report "
          "| live line - FSDS line |")
    print("|---|---|---|---|---|---|---|")
    for name, n_live, n_fsds, fsds_only, only, same_report, offsets in positions:
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(only.items())) or "-"
        lines = ", ".join(f"+{k}: {v}" for k, v in sorted(offsets.items()))
        print(f"| {name} | {n_live} | {n_fsds} | {fsds_only} | {kinds} | {same_report}/{n_fsds} "
              f"| {lines} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
