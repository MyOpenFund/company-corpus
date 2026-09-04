"""Acquire one issuer end-to-end in DISCOVERY mode (dispatch + dedup, no download).

`acquire(..., download=False)` resolves the entity, runs every applicable backend
(national + filings.xbrl.org + Euronext complement + listing fallback), de-duplicates
across them, and writes the coverage report — but downloads no files. Use it to
*size* a run before committing the bytes. Flip to `download=True` to fetch the files
into `data/raw/<LEI>/<FAMILY>/<year>/` (Catana is ~257 docs / ~290 MB, so leave it
False for a quick demo). Network; writes only the entity index + coverage report.

    ./venv/bin/python examples/20_eu_acquire_discovery.py
"""
from __future__ import annotations

import tempfile

from company_corpus import Config, Fetcher
from company_corpus.eu.acquire import acquire

with tempfile.TemporaryDirectory() as tmp:           # leave the repo's data/ untouched
    cfg = Config(data_dir=tmp)
    summary = acquire([{"isin": "FR0010193052"}],    # Catana Group SA
                      fetcher=Fetcher(cfg), config=cfg, download=False)

print("entities  :", summary["entities"])
print("documents :", summary["documents"])
print("coverage  :", summary["coverage_path"])
print("errors    :", summary["errors"] or "none")
