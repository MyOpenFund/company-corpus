"""Dispatch by LISTING, not just home country — cover an issuer with no national OAM.

The Euronext notices feed is ISIN-keyed and ignores the MIC in the URL, so an issuer
whose home country has no backend (Bermuda-, Cyprus-, Luxembourg-domiciled…) is still
reachable: `acquire()` runs a listing fallback, and `EuronextSource(force_mic=…)` is
that mode directly. Each notice's issuer cell is verified (rejecting market-wide
"Multiple" noise), so a match is the real issuer, not a guess. Here we resolve a
Euronext-listed name and probe its notices in listing mode. Network.

    ./venv/bin/python examples/22_eu_listing_dispatch.py
"""
from __future__ import annotations

from collections import Counter

from company_corpus import Config, Fetcher
from company_corpus.eu.entities import resolve_entities
from company_corpus.eu.sources.oam_euronext import EuronextSource, _LISTING_MIC

cfg = Config()
fetcher = Fetcher(cfg)

# 2020 Bulkers Ltd — Bermuda-domiciled (no national EU backend), listed on Oslo/Euronext.
entity = resolve_entities([{"name": "2020 Bulkers Ltd", "country": "BM"}], fetcher=fetcher)[0]
print(f"{entity.name}  country={entity.country}  LEI={entity.lei}  isins={list(entity.isins)}")
print(f"home-country backend for {entity.country!r}: none -> falling back to listing probe\n")

src = EuronextSource(fetcher=fetcher, config=cfg, force_mic=_LISTING_MIC)   # listing mode
docs = src.discover(entity)
print(f"  {len(docs)} Euronext notices via listing dispatch")
print("  by doc_type:", dict(Counter(d.doc_type for d in docs)))
print("  errors recorded:", [e["context"] for e in src.errors] or "none")
