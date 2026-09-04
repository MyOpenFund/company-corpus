"""Exhaustive discovery via the quarterly full-index (incl. delisted filers).

Unlike per-issuer discovery, `EdgarFullIndex` reads the SEC's quarterly master index
— every filer that quarter — so it finds issuers the ticker map omits (delisted,
merged). Here it's filtered to one CIK + one form family. NOTE: this downloads a full
quarterly `master.idx` (tens of MB) — heavier than the other examples. Run:

    ./venv/bin/python examples/13_discover_index.py
"""
from __future__ import annotations

from company_corpus.config import Config
from company_corpus.http import Fetcher
from company_corpus.sources.edgar_index import EdgarFullIndex
from company_corpus.taxonomy import parse_scope

src = EdgarFullIndex(Fetcher(Config()))
recs = list(src.discover(2024, 1, scope=parse_scope("A"), ciks={"0000320193"}))
for r in recs[:5]:
    print(f"  {r.sec_form:8} {r.filing_date}  {r.accession}")
print(f"  ({len(recs)} family-A filings for CIK 0000320193 in 2024 Q1)")
