"""Acquire a small multi-country basket and read the coverage report.

`acquire()` writes `data/reports/eu_coverage.jsonl` — one row per entity:
`{lei, name, country, resolution, doc_count, doc_types, gap}`. The `gap` makes
incompleteness explicit — an issuer that resolved but returned no documents is
`no-documents` (never a silent omission); an unbindable spec is `unresolved-entity`.
Here we discover (no download) three issuers in three jurisdictions and print their
rows. Network; writes only the entity index + coverage report (to a temp dir).

    ./venv/bin/python examples/21_eu_coverage_report.py
"""
from __future__ import annotations

import json
import tempfile

from company_corpus import Config, Fetcher
from company_corpus.eu.acquire import acquire

BASKET = [
    {"isin": "FR0010193052"},                     # Catana Group SA      (FR / AMF)
    {"name": "Iberdrola SA", "country": "ES"},    # Iberdrola            (ES / CNMV)
    {"isin": "IE00BF0L3536"},                     # AIB Group            (IE / FCA NSM, via FIGI bridge)
]

with tempfile.TemporaryDirectory() as tmp:
    cfg = Config(data_dir=tmp)
    summary = acquire(BASKET, fetcher=Fetcher(cfg), config=cfg, download=False)
    with open(summary["coverage_path"], encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            print(f"  {row.get('name', '?'):28} {row.get('country', '?'):3} "
                  f"docs={row.get('doc_count', 0):4}  gap={row.get('gap', '?'):18} "
                  f"types={','.join(row.get('doc_types', [])) or '-'}")
