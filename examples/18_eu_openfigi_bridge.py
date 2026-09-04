"""The OpenFIGI ISIN->LEI bridge — resolve issuers GLEIF's ISIN filter misses.

GLEIF's ISIN->LEI mapping is incomplete: it can hold an issuer's LEI yet not its
equity ISIN, so a plain `filter[isin]` lookup returns nothing. On that miss,
`resolve_entities` bridges ISIN -> OpenFIGI issuer name -> GLEIF (binding only on a
single normalised match) and tags the result `resolution="isin-figi"`. These two
Irish bank ISINs typically resolve *only* thanks to that bridge. Network.

    ./venv/bin/python examples/18_eu_openfigi_bridge.py
"""
from __future__ import annotations

from company_corpus import Config, Fetcher
from company_corpus.eu.entities import resolve_entities

cfg = Config()
fetcher = Fetcher(cfg)

for isin in ["IE00BF0L3536", "IE00BD1RP616"]:   # AIB Group, Bank of Ireland Group
    e = resolve_entities([{"isin": isin}], fetcher=fetcher)[0]
    print(f"  {isin} -> {e.name or '(unresolved)':28}  LEI={e.lei}  tier={e.resolution}")
