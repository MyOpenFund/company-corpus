"""Resolve company names to CIKs via the SEC cik-lookup-data.txt index.

The current ticker map lists only *trading* issuers, so delisted/renamed members
don't resolve by ticker. The name tier matches on a strict canonical name against
the SEC's full name->CIK file (former names included), and breaks a name borne by
two companies using their dated formerNames. Fully offline (a tiny inline index).

    ./venv/bin/python examples/15_name_resolution.py
"""
from __future__ import annotations

from company_corpus.sources.cik_lookup import parse_cik_lookup
from company_corpus.universe import resolve_names

# A slice of cik-lookup-data.txt: APPLE (one CIK) and a recycled "SUNRISE" name.
index = parse_cik_lookup(
    "APPLE INC:0000320193:\n"
    "APPLE COMPUTER INC:0000320193:\n"   # former name -> same CIK
    "SUNRISE CORP:0000111111:\n"
    "SUNRISE CORPORATION:0000222222:\n"  # same canonical name -> collision
)

resolved, collisions, unresolved = resolve_names(
    ["Apple Computer, Inc.", "Sunrise Corp", "Nonesuch Holdings"], index)

print("resolved   :", resolved)       # Apple's old name -> current CIK
print("collisions :", collisions)     # Sunrise -> two CIKs (needs a date to break)
print("unresolved :", unresolved)     # not in the index
