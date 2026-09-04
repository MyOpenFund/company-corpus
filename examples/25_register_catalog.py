"""Tour two of the register-financials pillar's eight sources (offline).

The pillar covers eight national registers (NO, UK, BE, LU, FI, DK, EE, SK — see
docs/REGISTER_FINANCIALS.md); this example walks two acquisition paths in full:
(1) Norway Brreg structured JSON -> map_brreg_entry; (2) UK Companies House
iXBRL -> oim_from_ch_html + map_ch_facts. Output lives in
data/financials_register/, separate from the EU and SEC pillars. Mirrors the
style of example 24 but for the register pillar. No network required.

    ./venv/bin/python examples/25_register_catalog.py
"""
from __future__ import annotations

from company_corpus.registers.concepts_no import NO_FIELDS
from company_corpus.registers.concepts_uk import UK_FIELDS

print("Register-financials pillar — two of eight sources, one curated schema")
print("=" * 62)

print("\n1. Norway — Brønnøysund Register Centre (Brreg)")
print("   URL    : https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}")
print("   Auth   : none (public JSON API, no key)")
print("   Parser : map_brreg_entry — direct field->key lookup (no XBRL / Arelle)")
print("   Basis  : SELSKAP -> 'company'  |  KONSERN -> 'consolidated'")
print("   GAAP   : N-GAAP (Norwegian Generally Accepted Accounting Principles)")
print("   Output : data/financials_register/<orgnr>.jsonl  (country='NO', source='brreg')")

print("\n2. UK — Companies House Accounts Bulk Data")
print("   URL    : monthly bulk ZIP (Accounts_Monthly_Data-YYYY-MM.zip)")
print("   Auth   : none (public bulk download, no key)")
print("   Parser : oim_from_ch_html (Arelle iXBRL) + flatten_oim_json + map_ch_facts")
print("   Basis  : 'company' only (consolidated detection deferred)")
print("   GAAP   : FRS 105 / FRS 102 / IFRS (FRC taxonomies)")
print("   Output : data/financials_register/<ch_number>.jsonl  (country='GB', source='companies_house')")

print("\n3. Separate output — never merged with EU or SEC pillars")
print("   data/financials_register/<entity_id>.jsonl  — curated rows (reported + derived)")
print("   data/reports/register_coverage_<source>.jsonl — per-entity status (one file per source:")
print("   Compare: data/financials_eu/ (ESEF/IFRS), data/financials/ (SEC/US-GAAP)")

print(f"\nNorwegian curated concept pack — NO_FIELDS ({len(NO_FIELDS)} keys):")
for key, fields in NO_FIELDS.items():
    print(f"   {key:26} <- {', '.join(fields)}")

print(f"\nUK curated concept pack — UK_FIELDS ({len(UK_FIELDS)} keys):")
for key, fields in UK_FIELDS.items():
    print(f"   {key:26} <- {', '.join(fields)}")

print("\n4. Leverage caveat — liabilities-based gearing for both registers")
print("   NO : sumGjeld / sumKortsiktigGjeld / sumLangsiktigGjeld")
print("        = total liabilities, not pure borrowings (N-GAAP gearing)")
print("   UK : CurrentAssets − NetCurrentAssets  /  TALCL − NetAssets")
print("        = derived from structural anchors; emitted atomically or not at all")
print("   => debt_to_equity / debt_to_assets are total-liabilities-based ratios")
print("    brreg / companies_house / bnb / lbr / prh / erst-fsa / erst-ifrs / rik / registeruz)")
print("   See docs/REGISTER_FINANCIALS.md for full caveats")
