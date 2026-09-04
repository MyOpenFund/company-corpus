"""Resolve a CUSIP to its issuer's CIK via EDGAR full-text search (the --fts tier).

For a bond no offline tier resolves, EFTS finds the filing that lists the CUSIP and
returns that filer's CIK. The search is **restricted to issuer offering forms**
(424B/FWP/S-3): an unrestricted CUSIP search would rank fund *holders* (N-PORT/13F)
first, not the issuer. Run (hits efts.sec.gov):

    ./venv/bin/python examples/07_fts_resolution.py
"""
from __future__ import annotations

from company_corpus.config import Config
from company_corpus.http import Fetcher
from company_corpus.sources.edgar_fts import OFFERING_FORMS, EdgarFTS

fts = EdgarFTS(Fetcher(Config()))
print("offering forms searched:", OFFERING_FORMS)
for cusip in ["057224AZ0", "05565QDH8"]:   # Baker Hughes note; BP Capital Markets America note
    hit = fts.resolve(cusip)
    print(f"  {cusip} -> {hit if hit else '(no offering-form hit)'}")
