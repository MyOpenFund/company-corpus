"""Completeness matrix: downloaded vs. expected per issuer / form / year.

Parallels ``cb_corpus.completeness``. Central banks have fixed publication
calendars; SEC issuers have a deterministic cadence only for a few periodic
forms (one 10-K and three 10-Qs per fiscal year, one annual proxy). Everything
else (8-K, 6-K, registrations, ownership) is event-driven, so its expected count
is *unknown* and the matrix reports presence rather than a target.

Status per (cik, form, year) cell:
    ok       discovered >= expected, or expected unknown but discovered > 0
    partial  0 < discovered < expected
    missing  expected > 0 but discovered == 0
    unknown  expected unknown and discovered == 0
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date

from .config import Config, normalize_cik
from .storage import Storage
from .taxonomy import FormType

# Deterministic annual cadence for periodic forms; None == event-driven.
EXPECTED_PER_YEAR: dict[FormType, int | None] = {
    FormType.A1: 1,   # 10-K
    FormType.A2: 3,   # 10-Q (three per fiscal year; Q4 is rolled into the 10-K)
    FormType.A3: 1,   # 20-F
    FormType.A4: 1,   # 40-F
    FormType.C1: 1,   # DEF 14A
}


def expected_count(form: FormType) -> int | None:
    """Expected filings per year for a form, or None if its cadence is event-driven.

    The cadence is the same for every year (no IPO/delisting pro-rating), so this
    takes no year -- it would imply a year-specific behavior that does not exist.
    """
    return EXPECTED_PER_YEAR.get(form)


def _status(discovered: int, expected: int | None) -> str:
    if expected is None:
        return "ok" if discovered > 0 else "unknown"
    if discovered >= expected:
        return "ok"
    if discovered > 0:
        return "partial"
    return "missing"


def build_matrix(
    ciks: Iterable[str],
    years: Sequence[int],
    scope: Sequence[FormType],
    storage: Storage | None = None,
    config: Config | None = None,
) -> list[dict]:
    """Return matrix rows for every (cik, form, year) in the requested grid."""
    storage = storage or Storage(config)
    rows: list[dict] = []
    year_set = set(years)

    for cik in ciks:
        cik = normalize_cik(cik)
        manifest = storage.load_manifest(cik)
        # Count discovered docs per (form, year).
        counts: dict[tuple[FormType, int], int] = defaultdict(int)
        # Label the issuer with its CURRENT name, taken from the most recent filing
        # — not whichever record is first in the manifest (the oldest, which would
        # carry a stale former name, e.g. "APPLE COMPUTER INC" on 2024 rows).
        latest = None
        for rec in manifest.values():
            if latest is None or (rec.filing_date or date.min) >= (latest.filing_date or date.min):
                latest = rec
            if rec.year is None:
                continue
            counts[(rec.form_type, rec.year)] += 1
        company = (latest.company_current or latest.company) if latest else ""

        for form in scope:
            for year in sorted(year_set):
                discovered = counts.get((form, year), 0)
                exp = expected_count(form)
                rows.append(
                    {
                        "cik": cik,
                        "company": company,
                        "form_type": form.code,
                        "sec_forms": ",".join(form.edgar_forms),
                        "year": year,
                        "expected": exp,
                        "discovered": discovered,
                        "status": _status(discovered, exp),
                    }
                )
    return rows


def summarize(rows: Iterable[dict]) -> dict[str, int]:
    """Tally status counts across matrix rows."""
    tally: dict[str, int] = defaultdict(int)
    for row in rows:
        tally[row["status"]] += 1
    return dict(tally)
