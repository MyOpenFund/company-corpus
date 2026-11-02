"""Synthetic EDGAR payloads for the store-batches tests (adversarial by design)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from company_corpus.config import Config, normalize_cik
from company_corpus.opendata.submissions import SubmissionsArchive, fetch_archive
from tests.conftest import FakeFetcher

FETCHED = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def submissions_main(cik="0000000042", *, name="ACME CORP", tickers=("ACME",), former=(),
                     filings=(), files=(), foreign_code=None) -> dict:
    """filings: (accession, acceptanceDateTime, form) triples; former: (name, from, to)."""
    business = ({"isForeignLocation": 1, "countryCode": foreign_code, "stateOrCountry": foreign_code}
                if foreign_code else {"isForeignLocation": 0, "stateOrCountry": "WA"})
    return {
        "cik": str(int(cik)), "name": name, "tickers": list(tickers),
        "formerNames": [{"name": n, "from": f, "to": t} for n, f, t in former],
        "addresses": {"business": business, "mailing": business},
        "filings": {
            "recent": {
                "accessionNumber": [a for a, _, _ in filings],
                "acceptanceDateTime": [t for _, t, _ in filings],
                "form": [f for _, _, f in filings],
            },
            "files": [{"name": n} for n in files],
        },
    }


def write_archive(config: Config, cik: str, main: dict, *, pages: dict | None = None,
                  now: datetime = FETCHED) -> SubmissionsArchive:
    routes = {f"submissions/CIK{normalize_cik(cik)}.json": json.dumps(main)}
    for name, body in (pages or {}).items():
        routes[f"submissions/{name}"] = json.dumps(body)
    return fetch_archive(config, cik, FakeFetcher(routes, config=config), now=now)


def point(val, end, *, start=None, accn="0000000042-24-000001", form="10-K",
          filed="2024-07-30", fy=2024, fp="FY") -> dict:
    p = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start is not None:
        p["start"] = start
    return p


def companyfacts(us_gaap: dict, dei: dict | None = None) -> dict:
    """us_gaap / dei: {tag: {unit: [points]}}."""
    def ns(tags):
        return {t: {"label": t, "units": units} for t, units in (tags or {}).items()}
    return {"cik": 42, "entityName": "ACME CORP", "facts": {"us-gaap": ns(us_gaap), "dei": ns(dei)}}


def write_companyfacts(config: Config, cik: str, facts: dict) -> Path:
    path = config.raw_dir / normalize_cik(cik) / "F1" / "companyfacts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(facts), encoding="utf-8")
    return path
