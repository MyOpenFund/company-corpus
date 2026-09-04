"""Fetch Equinor's statutory accounts from Brreg and print a multi-year summary.

Norway's Brønnøysund Register Centre serves annual statutory accounts as open
JSON (no API key). build_register_financials writes curated rows to a temporary
data/financials_register/<orgnr>.jsonl. The print shows SELSKAP (standalone
legal entity) vs KONSERN (consolidated group) side-by-side across multiple years.
Leverage note: D/E here is liabilities-based (N-GAAP), not pure-borrowings.
Network (Brreg open JSON — no key required).

    export COMPANY_CORPUS_CONTACT="you@example.com"
    ./venv/bin/python examples/26_no_brreg_financials.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from company_corpus import Config, Fetcher
from company_corpus.registers.financials import build_register_financials

EQUINOR_ORGNR = "923609016"   # Equinor ASA


def _fmtn(v: float | None) -> str:
    """Format a large NOK value compactly."""
    if v is None:
        return "n/a"
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M"
    return f"{v:,.0f}"


def _fmtr(v: float | None) -> str:
    """Format a ratio (e.g. debt_to_equity)."""
    return f"{v:.2f}" if v is not None else "n/a"


with tempfile.TemporaryDirectory() as tmp:
    cfg = Config(data_dir=tmp, verify_tls=False)   # verify_tls=False for SSL-inspection proxies
    fetcher = Fetcher(cfg)

    rep = build_register_financials(
        [{"orgnr": EQUINOR_ORGNR}],
        fetcher=fetcher,
        config=cfg,
        write=True,
    )

    print(f"Equinor ASA — orgnr {EQUINOR_ORGNR}  (source: Brreg open JSON)")
    print(f"  entities: {rep['entities']}  "
          f"with_financials: {rep['with_financials']}  "
          f"periods: {rep['periods']}  "
          f"errors: {rep['errors']}")

    for rel_path in rep.get("paths", []):
        rows = [json.loads(line) for line in (cfg.data_dir / rel_path).read_text().splitlines() if line]

        # Group rows by (basis, fy); collect concept->value per group
        by_period: dict[tuple, dict[str, float]] = {}
        for r in rows:
            key = (r["basis"], r["fy"])
            by_period.setdefault(key, {})[r["concept"]] = r["value"]

        print(f"\n  {'basis':>12}  {'FY':4}  "
              f"{'revenue':>10}  {'net_income':>10}  "
              f"{'equity':>10}  {'D/E (liab.)':>12}")
        for (basis, fy), vals in sorted(by_period.items()):
            rev = vals.get("revenue")
            ni  = vals.get("net_income")
            eq  = vals.get("equity")
            dte = vals.get("debt_to_equity")
            print(f"  {basis:>12}  {fy:4d}  "
                  f"{_fmtn(rev):>10}  {_fmtn(ni):>10}  "
                  f"{_fmtn(eq):>10}  {_fmtr(dte):>12}")

print()
print("Note: D/E = liabilities-based gearing (sumGjeld / sumEgenkapital) — N-GAAP register.")
print("      'company' = SELSKAP standalone entity; 'consolidated' = KONSERN group.")
