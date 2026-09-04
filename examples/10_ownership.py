"""Structure ownership filings — insider Forms 3/4/5 and 13F (family E).

`process_ownership` downloads each family-E filing and replaces its raw text with a
structured summary + normalized rows (`data/ownership/<cik>.jsonl`). It needs the
manifest populated first, so we discover family E for one issuer, then structure a
single filing. Bounded; writes to a temp dir. Run (hits SEC EDGAR):

    ./venv/bin/python examples/10_ownership.py
"""
from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

from company_corpus.config import Config
from company_corpus.pipeline import discover_universe, process_ownership
from company_corpus.taxonomy import parse_scope

cfg = Config(data_dir=Path(tempfile.mkdtemp()) / "data")
APPLE = "320193"

discover_universe([APPLE], scope=parse_scope("E"), since=date(2024, 1, 1), dry_run=False, config=cfg)
report = process_ownership([APPLE], since=date(2024, 1, 1), limit=1, dry_run=False, config=cfg)
print(f"downloaded={report.downloaded} insider(E1)={report.parsed_insider} "
      f"13F(E2)={report.parsed_13f} passthrough(E3)={report.passthrough}")

table = cfg.ownership_dir / "0000320193.jsonl"
if table.exists():
    print("first structured row:")
    print(" ", json.loads(table.read_text(encoding="utf-8").splitlines()[0]))
