"""List an issuer's family-A filings (10-K / 10-Q / 20-F / 40-F) — metadata only.

Discovery reads the EDGAR submissions API and yields FilingRecords; nothing is
downloaded or written to disk here. Run:

    ./venv/bin/python examples/02_discover_filings.py
"""
from __future__ import annotations

from datetime import date

from company_corpus import Config, Fetcher, parse_scope
from company_corpus.sources.edgar_submissions import EdgarSubmissions

APPLE_CIK = "320193"

cfg = Config()
fetcher = Fetcher(cfg)
source = EdgarSubmissions(fetcher, cfg)

# scope = the four periodic-report forms; since = a recent cutoff to keep it short.
records = source.discover(APPLE_CIK, scope=parse_scope("A"), since=date(2023, 1, 1))

for rec in sorted(records, key=lambda r: r.filing_date or date.min, reverse=True):
    print(f"  {rec.filing_date}  {rec.sec_form:6} {rec.form_type.code}  {rec.accession}")
