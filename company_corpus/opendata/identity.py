"""EDGAR submissions archive -> my-open-data ``entity_observations`` rows (spec §5, S11).

EDGAR-only and minimal: the CIK itself (current and former names, country), and the tickers
(non-resolving in my-open-data, so they never merge entities). known_at is the archive's
fetch time (kind ``collected``): replaying an unchanged archive re-emits identical rows,
a refreshed archive is a new observation (SCD2 in the store).
Each refresh, explicit or automatic, adds identity rows at a new ``valid_date`` (about one
refresh per periodic filing; each adds one row per ticker and, for issuers without former
names, a CIK row): ``entity_observations`` is not one row per identifier, and
``entity_attributes`` collapses them.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from .submissions import SubmissionsArchive

SOURCE_CODE = "sec"


@dataclass
class IdentityResult:
    rows: list[dict]
    counters: Counter = field(default_factory=Counter)


def _day(value) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _country(main: dict, codes: Mapping[str, str], counters: Counter) -> str | None:
    """US for a US business address, else the EDGAR country code through ``codes``."""
    addresses = main.get("addresses") or {}
    address = addresses.get("business") or {}
    if address.get("isForeignLocation") is None:
        address = addresses.get("mailing") or {}
    if address.get("isForeignLocation") == 0 and address.get("stateOrCountry"):
        return "US"
    code = address.get("countryCode") or address.get("stateOrCountry")
    iso = codes.get(code) if code else None
    if iso is None:
        counters["country_unknown"] += 1
        if code:
            counters[f"country_unknown:{code}"] += 1
    return iso


def identity_rows(archive: SubmissionsArchive, *,
                  country_codes: Mapping[str, str]) -> IdentityResult:
    main, cik, counters = archive.main, archive.cik, Counter()
    known_at = archive.fetched_at
    fetched_day = known_at.date()
    base = {"subject_scheme": "cik", "subject_value": cik, "known_at": known_at,
            "known_at_kind": "collected", "source_code": SOURCE_CODE, "source_doc_id": None}
    by_key: dict[tuple, dict] = {}

    def put(key: tuple, row: dict) -> None:
        if key in by_key:
            counters["former_name_shadowed"] += 1
        by_key[key] = row

    formers = []
    for f in main.get("formerNames") or []:
        name, start = ((f.get("name") or "").strip(), _day(f.get("from"))) if isinstance(f, dict) \
            else ("", None)
        if not name or start is None:
            counters["former_name_undated"] += 1
            continue
        formers.append((start, _day(f.get("to")), name))
    # Ascending (from, to): of two names starting the same day, the longer-lived one is kept.
    for start, _, name in sorted(formers, key=lambda x: (x[0], x[1] or date.max, x[2])):
        put(("cik", start), {**base, "id_scheme": "cik", "id_value": cik, "name": name,
                             "country": None, "valid_date": start})
    ends = [end for _, end, _ in formers if end is not None]
    current_from = max(ends) if ends else fetched_day
    put(("cik", current_from), {**base, "id_scheme": "cik", "id_value": cik,
                                "name": (main.get("name") or "").strip() or None,
                                "country": _country(main, country_codes, counters),
                                "valid_date": current_from})
    for ticker in sorted({str(t).strip() for t in main.get("tickers") or []} - {""}):
        by_key[("ticker", ticker)] = {**base, "id_scheme": "ticker", "id_value": ticker,
                                      "name": None, "country": None, "valid_date": fetched_day}
    return IdentityResult(rows=list(by_key.values()), counters=counters)
