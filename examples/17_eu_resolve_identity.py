"""Resolve EU issuers to their GLEIF LEI — by ISIN and by name.

EU identity is the GLEIF Legal Entity Identifier (+ the issuer's ISINs) — the EU
analog of the SEC CIK. ``resolve_entities`` records HOW each one resolved (the
``resolution`` tier: lei / isin / isin-figi / name / unresolved), and an ambiguous
name is left unresolved rather than guessed. Network (GLEIF).

    ./venv/bin/python examples/17_eu_resolve_identity.py
"""
from __future__ import annotations

from company_corpus import Config, Fetcher
from company_corpus.eu.entities import resolve_entities

cfg = Config()
fetcher = Fetcher(cfg)

specs = [
    {"isin": "FR0000120271"},                    # TotalEnergies, by ISIN
    {"isin": "FR0010193052"},                    # Catana Group, by ISIN
    {"name": "CATANA GROUP", "country": "FR"},   # same issuer, by exact name
    {"name": "SAP SE", "country": "DE"},         # ambiguous in GLEIF -> unresolved
]

for e in resolve_entities(specs, fetcher=fetcher):
    print(f"  {e.name or '(unresolved)':30}  LEI={e.lei}  "
          f"country={e.country or '?':3}  tier={e.resolution:11}  #isins={len(e.isins)}")
