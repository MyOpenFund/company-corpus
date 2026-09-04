"""Parse Companies House iXBRL bulk accounts with Arelle and emit curated rows.

Builds a synthetic bulk ZIP from the two committed HTML fixture files, runs
build_ch_financials (Arelle parses each iXBRL filing), and prints the summary
+ emitted rows per entity. Real runs use the monthly Accounts_Monthly_Data-*.zip.
Requires Arelle (the optional eu-financials extra).

    ./venv/bin/python examples/29_uk_companies_house_financials.py
"""
from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

from company_corpus import Config
from company_corpus.registers.financials import build_ch_financials

# Two real iXBRL fixtures committed under tests/fixtures/uk/:
#   02855129 — FRS 105 micro-entity (balance-sheet only, period ended 2026-03-31)
#   SC741022 — FRS 102 small company (P&L + balance sheet, period ended 2025-08-31)
_REPO_ROOT   = Path(__file__).resolve().parent.parent
FIXTURE_DIR  = _REPO_ROOT / "tests" / "fixtures" / "uk"
MICRO_HTML   = FIXTURE_DIR / "frs105_micro_02855129.html"
PL_HTML      = FIXTURE_DIR / "frs102_pl_SC741022.html"

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)

    # Build a synthetic bulk ZIP (filename convention: Prod223_4212_<CH>_<YYYYMMDD>.html)
    zip_path = tmp_path / "accounts_bulk.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Prod223_4212_02855129_20260331.html", MICRO_HTML.read_bytes())
        zf.writestr("Prod223_4212_SC741022_20250831.html", PL_HTML.read_bytes())

    cfg = Config(data_dir=tmp_path)

    try:
        rep = build_ch_financials(str(zip_path), config=cfg, write=True)
    except ImportError:
        print("requires the optional Arelle extra: pip install '.[eu-financials]'")
        raise SystemExit(1)

    print("build_ch_financials — summary:")
    print(f"  entities       : {rep['entities']}")
    print(f"  with_financials: {rep['with_financials']}")
    print(f"  unbalanced     : {rep['unbalanced']}")
    print(f"  no_financials  : {rep['no_financials']}")
    print(f"  errors         : {rep['errors']}")
    print(f"  periods        : {rep['periods']}")

    for rel_path in sorted(rep.get("paths", [])):
        path = cfg.data_dir / rel_path
        ch_number = path.stem
        rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        if not rows:
            continue
        first = rows[0]
        reported = {r["concept"]: r["value"] for r in rows if r["kind"] == "reported"}
        print(f"\n  {ch_number}"
              f"  basis={first.get('basis', '?')}"
              f"  source={first.get('source', '?')}"
              f"  country={first.get('country', '?')}"
              f"  period_end={first.get('period_end', '?')}")
        for concept in ("assets", "equity", "revenue", "net_income", "cash"):
            v = reported.get(concept)
            if v is not None:
                print(f"    {concept:15} {v:>12,.0f}  {first.get('currency', 'GBP')}")
