"""Enrich + triage identifiers via OpenFIGI (the enrich-openfigi command).

OpenFIGI maps an ISIN/CUSIP to issuer name / ticker / security type — but NOT a CIK,
so it's a triage aid, not a resolver. `coverage_hint` labels each security
jurisdiction-neutrally: `registry_candidate` (publicly registered — worth chasing in
a filings registry) vs `private_placement` (144A/Reg-S — reachable nowhere). Free,
no key needed (a key raises rate limits). Run (hits api.openfigi.com):

    ./venv/bin/python examples/09_openfigi_enrich.py
"""
from __future__ import annotations

from company_corpus.openfigi import coverage_hint, map_identifiers

isins = ["US00037BAC63", "US350930AB92", "US46115HBQ92"]  # ABB Finance; Foundry JV (144A); Intesa (144A)
for ident, rec in map_identifiers(isins, id_type="isin").items():
    if rec:
        print(f"  {ident}  {rec.name[:30]:30} {rec.security_type:14} -> {coverage_hint(rec.security_type)}")
    else:
        print(f"  {ident}  (no match)")
