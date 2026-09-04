"""Build a universe from a CSV of identifiers (the credit-universe path).

`reconcile_identifiers` resolves each row by authority **CIK > CUSIP6 > ticker**:
an explicit CIK wins; otherwise ticker->CIK and CUSIP6->CIK must agree, and when
they DISAGREE it's a collision (a recycled ticker pointing at a different company
than the bond's CUSIP6 — e.g. DT->Dynatrace, not Deutsche Telekom).

This demo is fully offline (a hand-built ticker map + crosswalk); in real use the
ticker map comes from `load_company_tickers(fetcher)` and the crosswalk from a
`cik,cusip6` CSV passed as `--crosswalk`. Run:

    ./venv/bin/python examples/06_universe_from_file.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from company_corpus.universe import Issuer, read_identifier_csv, reconcile_identifiers

# Normally: load_company_tickers(Fetcher(Config())) — inlined here for an offline demo.
ticker_table = {
    "AAPL": Issuer(cik="320193", ticker="AAPL", company="Apple Inc."),
    "DT": Issuer(cik="1773383", ticker="DT", company="Dynatrace, Inc."),
}
crosswalk = {"037833": {"0000320193"}, "25156P": {"0000999999"}}  # CUSIP6 -> {CIK}

csv = Path(tempfile.mkdtemp()) / "universe.csv"
csv.write_text(
    "CIK,Ticker,CUSIP,Issuer\n"
    "0000789019,MSFT,,Microsoft (CIK is authoritative)\n"   # CIK column wins
    ",AAPL,037833AA0,Apple Inc\n"                            # ticker & CUSIP6 agree
    ",DT,25156PAA0,Deutsche Telekom Intl Finance\n"          # recycled-ticker collision
    ",NOPE,,Mystery Co\n",                                   # nothing resolves
    encoding="utf-8",
)

rows = read_identifier_csv(csv)
issuers, collisions, unresolved = reconcile_identifiers(rows, ticker_table, crosswalk)

print("resolved:")
for it in issuers:
    print(f"  {it.ticker or '(no ticker)':12} -> CIK {it.cik}  [{it.resolution}]")
print("collisions (ticker vs CUSIP6 disagree):")
for c in collisions:
    print(f"  {c['ticker']:12} ticker->{c['cik_ticker']} cusip->{c['cik_cusip']}  [{c['kind']}]")
print("unresolved:", ", ".join(unresolved) or "(none)")
