"""Resolve tickers to SEC CIKs via the official company-tickers map.

The CIK is EDGAR's permanent issuer key (it never changes on rename), so every
other operation is keyed on it. Run:

    ./venv/bin/python examples/01_resolve_universe.py
"""
from __future__ import annotations

from company_corpus import Config, Fetcher, resolve_tickers

cfg = Config()  # reads COMPANY_CORPUS_CONTACT for the User-Agent, if set
print("User-Agent:", cfg.user_agent)

fetcher = Fetcher(cfg)
issuers, unresolved = resolve_tickers(["AAPL", "MSFT", "GOOGL", "NOPE"], fetcher)

for it in issuers:
    print(f"  {it.ticker:6} -> CIK {it.cik}  {it.company}")
if unresolved:
    print("  unresolved (delisted/renamed?):", ", ".join(unresolved))
