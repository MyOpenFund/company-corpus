"""Audit coverage with the completeness matrix (issuer x form x year).

`build_matrix` compares what's discovered against an expected cadence (e.g. one 10-K
and ~three 10-Q per year) and flags each cell ok / partial / missing / unknown, so
gaps are explicit rather than assumed. We discover family A for one issuer, then build
the matrix. Bounded; writes to a temp dir. Run (hits SEC EDGAR):

    ./venv/bin/python examples/14_completeness_report.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from company_corpus.completeness import build_matrix, summarize
from company_corpus.config import Config
from company_corpus.pipeline import discover_universe
from company_corpus.storage import Storage
from company_corpus.taxonomy import parse_scope

cfg = Config(data_dir=Path(tempfile.mkdtemp()) / "data")
discover_universe(["320193"], scope=parse_scope("A"), dry_run=False, config=cfg)

rows = build_matrix(["320193"], [2022, 2023, 2024], parse_scope("A"), Storage(cfg), cfg)
for r in rows:
    if r.get("status") != "unknown":
        print(f"  {r}")
print("summary:", summarize(rows))
