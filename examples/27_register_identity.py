"""Resolve register specs to canonical entity keys across NO and GB (GLEIF network).

Identity is resolved without guessing: an orgnr or ch_number supplied directly
passes through with no network call. A GLEIF LEI is followed only to the
registeredAs key of its legal-address country — NO -> orgnr, GB -> ch_number;
any other jurisdiction returns status='unresolved'. Shows all four paths plus
the offline _norm_ch_number helper that zero-pads digits and preserves SC/OC
prefixes. Network (GLEIF — two lookups for the two LEI specs).

    export COMPANY_CORPUS_CONTACT="you@example.com"
    ./venv/bin/python examples/27_register_identity.py
"""
from __future__ import annotations

from company_corpus import Config, Fetcher
from company_corpus.registers.identity import _norm_ch_number, resolve_register_specs

# ---------------------------------------------------------------------------
# 1. Offline: _norm_ch_number — CH-number normalisation
# ---------------------------------------------------------------------------
print("_norm_ch_number — normalise a Companies House number (offline):")
_cases = [
    ("510976",     "00510976",  "pure digits -> zero-pad to 8"),
    ("SC741022",   "SC741022",  "Scottish (SC) prefix -> preserved verbatim"),
    (" oc372294 ", "OC372294",  "LLP (OC) prefix -> strip whitespace + uppercase"),
]
for raw, expected, note in _cases:
    result = _norm_ch_number(raw)
    assert result == expected, f"{raw!r} -> {result!r}, expected {expected!r}"
    print(f"  {raw!r:16} -> {result!r:12}  ({note})")

# ---------------------------------------------------------------------------
# 2. Network: resolve four specs across both registers
# ---------------------------------------------------------------------------
cfg = Config()
fetcher = Fetcher(cfg)

specs = [
    # (a) direct orgnr — no network; resolves immediately
    {"orgnr": "923609016"},
    # (b) NO LEI (Equinor ASA) — GLEIF registeredAs -> orgnr 923609016 (NO path)
    {"lei": "OW6OFBNCKXC4US5C7523"},
    # (c) GB LEI (Lloyds Banking Group) — GLEIF registeredAs -> ch_number 10399850 (GB path)
    {"lei": "213800MBWEIJDM5CU638"},
    # (d) DE LEI (Deutsche Bank) — country not NO or GB -> unresolved
    {"lei": "7LTWFZYICNSX8D621K86"},
]

print(f"\nresolve_register_specs — {len(specs)} specs "
      f"(2 GLEIF lookups; direct orgnr is offline):")
results = resolve_register_specs(specs, fetcher=fetcher)
for spec, r in zip(specs, results):
    # resolved_id: prefer ch_number (GB), then orgnr (NO), then show unresolved
    if r.get("ch_number"):
        resolved = f"ch:{r['ch_number']}"
        id_type  = "ch_number"
    elif r.get("orgnr"):
        resolved = f"no:{r['orgnr']}"
        id_type  = "orgnr"
    else:
        resolved = "(unresolved)"
        id_type  = "—"

    name_clip = (r.get("name") or "")[:28]
    print(f"  {str(spec):42}  "
          f"-> {resolved:18}  "
          f"key={id_type:10}  "
          f"country={r.get('country') or '?':3}  "
          f"status={r['status']:11}  "
          f"name={name_clip!r}")
