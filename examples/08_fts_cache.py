"""Persist --fts resolutions to a reusable CUSIP6->CIK cache (the --fts-cache file).

`write_cusip_crosswalk` merges (cik, cusip6) pairs into a `cik,cusip6` CSV (deduped);
`load_cusip_crosswalk` reads it back. `build-universe --fts-cache FILE` uses exactly
this format: confirmed resolutions are appended, and on the next run the file is read
into the offline crosswalk so those issuers resolve WITHOUT hitting EFTS again.
Fully offline. Run:

    ./venv/bin/python examples/08_fts_cache.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from company_corpus.universe import load_cusip_crosswalk, write_cusip_crosswalk

cache = Path(tempfile.mkdtemp()) / "fts_cache.csv"

n = write_cusip_crosswalk(cache, [("0000808362", "057224"), ("0001167583", "05565Q")])
print(f"run 1 wrote {n} pairs")

# A later run merges new confirmed pairs (and dedups the ones already present).
n = write_cusip_crosswalk(cache, [("808362", "057224"), ("0000999999", "25156P")])
print(f"run 2 -> {n} pairs total (1 new, 1 duplicate dropped)")

print("cache reads back as CUSIP6 -> {CIK}:")
for c6, ciks in sorted(load_cusip_crosswalk(cache).items()):
    print(f"  {c6} -> {sorted(ciks)}")
