"""Print the EU coverage catalog — backends, Euronext markets, doc-type taxonomy.

A fully OFFLINE tour of what the EU pillar covers: the national `COUNTRY_BACKENDS`
map (country -> OamSource), the `EURONEXT_MICS` markets the Euronext complement
reaches, and the controlled `DOC_TYPES` vocabulary with the `DOC_FAMILY` that maps
each to its on-disk family folder. Adding a country = one row in COUNTRY_BACKENDS.

    ./venv/bin/python examples/24_eu_backends_catalog.py
"""
from __future__ import annotations

from company_corpus.eu.acquire import COUNTRY_BACKENDS
from company_corpus.eu.documents import DOC_FAMILY, DOC_TYPES
from company_corpus.eu.sources.oam_euronext import EURONEXT_MICS

print(f"National backends ({len(COUNTRY_BACKENDS)} jurisdictions):")
for country, cls in sorted(COUNTRY_BACKENDS.items()):
    print(f"   {country}  ->  {cls.__name__}")

print(f"\nEuronext markets (complement + listing fallback) — {len(EURONEXT_MICS)}:")
for country, mic in EURONEXT_MICS.items():
    print(f"   {country}  ->  {mic}")

print(f"\nplus filings.xbrl.org (ESEF complement, by LEI)")

print(f"\nDoc-type taxonomy ({len(DOC_TYPES)} types -> on-disk family):")
for dt in DOC_TYPES:
    print(f"   {dt:22} -> {DOC_FAMILY[dt]}")
