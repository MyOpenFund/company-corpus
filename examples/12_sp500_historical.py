"""Build the S&P 500 as a *historical union* (not just today's members).

`issuers_from_sp500(start=...)` reconstructs membership from Wikipedia (current table
+ the dated changes table), so the result is every company that was a member over the
window — with `first_seen`/`last_seen` dates — not survivorship-biased on selection.
Run (hits Wikipedia):

    ./venv/bin/python examples/12_sp500_historical.py
"""
from __future__ import annotations

from company_corpus.config import Config
from company_corpus.http import Fetcher
from company_corpus.universe import issuers_from_sp500

issuers, changes, unresolved = issuers_from_sp500(Fetcher(Config()), start="2015")
print(f"{len(issuers)} historical members since 2015; {len(changes)} dated changes; "
      f"{len(unresolved)} members without a resolvable CIK")
for it in issuers[:5]:
    print(f"  {it.ticker:6} {it.cik or '(unresolved)':10}  first_seen={it.first_seen} last_seen={it.last_seen}")
