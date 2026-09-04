"""Calibration tool: parse a local ESEF .zip with Arelle and optionally compare
to Tier A (filings.xbrl.org json_url) for the same issuer.

Usage
-----
# Parse only — print headline reported values:
    ./venv/bin/python scripts/calibrate_arelle.py --zip path/to/report.zip

# Full calibration — cross-check Arelle vs filings.xbrl.org for the same issuer:
    ./venv/bin/python scripts/calibrate_arelle.py --zip path/to/report.zip \\
        --lei <LEI> --contact you@example.com

Arelle must be installed (pip install '.[eu-financials]'). The script raises a
clear ImportError with an install hint if it is absent.

This script is a calibration / smoke-check tool. It does NOT write any corpus
data; it is safe to run repeatedly against a local zip and does not modify disk
state (beyond Arelle's own taxonomy cache, which it manages itself).
"""
from __future__ import annotations

import argparse
import sys

HEADLINE = ["revenue", "operating_income", "net_income", "assets", "equity", "cash"]


def _summaries_from_zip(zip_path: str, *, entity_name: str = ""):
    """Parse zip with Arelle -> flatten -> summaries. Returns list[Summary]."""
    from company_corpus.eu.arelle_esef import oim_from_esef_zip
    from company_corpus.xbrl import flatten_oim_json
    from company_corpus.xbrl import IFRS_CONCEPTS, IFRS_CONCEPTS_BY_KEY
    from company_corpus.financials import summaries_from_flat, attach_ttm_from_flat

    print(f"[Tier B] Parsing {zip_path} with Arelle …")
    report = oim_from_esef_zip(zip_path)
    flat = flatten_oim_json(report, filed="", form="annual_report", accn="arelle-local")
    mapped = sum(1 for c in IFRS_CONCEPTS if any(t in flat for t in c.tags))
    print(f"[Tier B] IFRS concepts mapped: {mapped}/{len(IFRS_CONCEPTS)}")
    summaries = summaries_from_flat(
        flat, concepts=IFRS_CONCEPTS,
        company=entity_name or "(unknown)", company_current=entity_name or "(unknown)",
        sic=None,
    )
    attach_ttm_from_flat(flat, summaries, concepts_by_key=IFRS_CONCEPTS_BY_KEY)
    return summaries


def _summaries_from_tier_a(lei: str, *, fetcher, entity_name: str = ""):
    """Fetch Tier-A facts from filings.xbrl.org -> summaries. Returns list[Summary]."""
    from company_corpus.eu.entities import Entity
    from company_corpus.eu.financials import facts_for_entity
    from company_corpus.xbrl import IFRS_CONCEPTS, IFRS_CONCEPTS_BY_KEY
    from company_corpus.financials import summaries_from_flat, attach_ttm_from_flat

    ent = Entity(lei=lei, name=entity_name or lei, country="", resolution="manual")
    print(f"[Tier A] Fetching filings.xbrl.org facts for {lei} …")
    flat = facts_for_entity(ent, fetcher=fetcher)
    mapped = sum(1 for c in IFRS_CONCEPTS if any(t in flat for t in c.tags))
    print(f"[Tier A] IFRS concepts mapped: {mapped}/{len(IFRS_CONCEPTS)}")
    summaries = summaries_from_flat(
        flat, concepts=IFRS_CONCEPTS,
        company=entity_name or lei, company_current=entity_name or lei,
        sic=None,
    )
    attach_ttm_from_flat(flat, summaries, concepts_by_key=IFRS_CONCEPTS_BY_KEY)
    return summaries


def _print_summaries(summaries, label: str, n: int = 4) -> None:
    print(f"\n{'='*60}")
    print(f"  {label} — {len(summaries)} period(s) found")
    print(f"{'='*60}")
    for s in summaries[:n]:
        print(f"\n  {s.period_label} [{s.currency}]")
        for k in HEADLINE:
            v = s.values.get(k)
            print(f"    {k:20} {v['value'] if v else '—'}")


def _compare(tier_b_summaries, tier_a_summaries) -> None:
    """Print a per-period, per-concept MATCH/DIFF table."""
    print(f"\n{'='*60}")
    print("  CALIBRATION — Arelle vs filings.xbrl.org")
    print(f"{'='*60}")

    # Index Tier A by period_label for easy lookup
    tier_a_by_period = {s.period_label: s for s in tier_a_summaries}

    any_diff = False
    for sb in tier_b_summaries:
        sa = tier_a_by_period.get(sb.period_label)
        print(f"\n  Period: {sb.period_label} [{sb.currency}]")
        if sa is None:
            print("    (period not found in Tier A — cannot compare)")
            continue
        for k in HEADLINE:
            vb = sb.values.get(k)
            va = sa.values.get(k)
            bv = vb["value"] if vb else None
            av = va["value"] if va else None
            if bv is None and av is None:
                status = "BOTH_MISSING"
            elif bv is None:
                status = "DIFF (Arelle=missing, TierA={})".format(av)
                any_diff = True
            elif av is None:
                status = "DIFF (Arelle={}, TierA=missing)".format(bv)
                any_diff = True
            elif bv == av:
                status = "MATCH"
            else:
                pct = abs(bv - av) / max(abs(av), 1) * 100
                status = f"DIFF  Arelle={bv}  TierA={av}  ({pct:.1f}% delta)"
                any_diff = True
            print(f"    {k:20} {status}")

    print()
    if any_diff:
        print("RESULT: DIFF(s) detected — review the deltas above.")
        print("        Small deltas may be rounding; large deltas warrant investigation.")
    else:
        print("RESULT: MATCH — Arelle and filings.xbrl.org facts agree on all compared concepts.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Calibrate Tier B (Arelle) against a local ESEF .zip. "
            "With --lei, also fetches Tier-A facts from filings.xbrl.org "
            "and prints a MATCH/DIFF comparison."
        )
    )
    ap.add_argument("--zip", required=True, metavar="ESEF_ZIP_PATH",
                    help="Path to a local ESEF report-package .zip file.")
    ap.add_argument("--lei", default=None, metavar="LEI",
                    help="Optional LEI for the Tier-A cross-check (requires network).")
    ap.add_argument("--contact", default=None, metavar="EMAIL",
                    help="Contact address for the HTTP User-Agent (required for Tier-A fetch).")
    ap.add_argument("--insecure", action="store_true",
                    help="Disable TLS verification (use only behind a trusted proxy).")
    args = ap.parse_args()

    # ---- Tier B: always ----
    try:
        tier_b = _summaries_from_zip(args.zip)
    except ImportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"ERROR parsing zip: {exc}", file=sys.stderr)
        return 1

    _print_summaries(tier_b, label=f"Tier B (Arelle) — {args.zip}")

    # ---- Tier A cross-check: only when --lei is given ----
    if args.lei:
        from company_corpus.config import Config
        from company_corpus.http import Fetcher

        cfg_kwargs: dict = {"verify_tls": not args.insecure}
        if args.contact:
            cfg_kwargs["contact"] = args.contact
        cfg = Config(**cfg_kwargs)
        fetcher = Fetcher(cfg)
        try:
            tier_a = _summaries_from_tier_a(args.lei, fetcher=fetcher)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR fetching Tier A for {args.lei}: {exc}", file=sys.stderr)
            return 1

        _print_summaries(tier_a, label=f"Tier A (filings.xbrl.org) — {args.lei}")
        _compare(tier_b, tier_a)
    else:
        print("\n(Pass --lei <LEI> to also run the Tier A cross-check.)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
