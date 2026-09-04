"""Live validation: run EU Pillar-B financials for one LEI and print headline values.

Usage:
    ./venv/bin/python scripts/validate_eu_financials.py <LEI> [--contact you@example.com]

Pick a large, clean ifrs-full filer indexed on filings.xbrl.org and eyeball the
printed revenue / profit / equity / assets against its published annual report.
"""
import argparse

from company_corpus.config import Config
from company_corpus.http import Fetcher
from company_corpus.eu.entities import resolve_entities
from company_corpus.eu.financials import facts_for_entity
from company_corpus.xbrl import IFRS_CONCEPTS, IFRS_CONCEPTS_BY_KEY
from company_corpus.financials import summaries_from_flat, attach_ttm_from_flat

HEADLINE = ["revenue", "operating_income", "net_income", "assets", "equity", "cash"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lei")
    ap.add_argument("--contact", default=None)
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()
    cfg = Config(**{k: v for k, v in
                    {"contact": args.contact, "verify_tls": not args.insecure}.items()
                    if v not in (None, True) or k == "verify_tls"})
    fetcher = Fetcher(cfg)
    ent = resolve_entities([{"lei": args.lei}], fetcher=fetcher)[0]
    print(f"entity: {ent.name} | {ent.lei} | {ent.country} | resolution={ent.resolution}")
    flat = facts_for_entity(ent, fetcher=fetcher)
    print(f"distinct ifrs concepts mapped: "
          f"{sum(1 for c in IFRS_CONCEPTS if any(t in flat for t in c.tags))}/{len(IFRS_CONCEPTS)}")
    summaries = summaries_from_flat(flat, concepts=IFRS_CONCEPTS, company=ent.name,
                                    company_current=ent.name, sic=None)
    attach_ttm_from_flat(flat, summaries, concepts_by_key=IFRS_CONCEPTS_BY_KEY)
    for s in summaries[:4]:
        print(f"\n{s.period_label} [{s.currency}]")
        for k in HEADLINE:
            v = s.values.get(k)
            print(f"  {k:18} {v['value'] if v else '—'}")
        d = s.derived
        for k in ("ebitda", "net_debt", "roe", "net_margin"):
            if k in d:
                print(f"  {k:18} {d[k]['value']:.2f} {d[k]['unit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
