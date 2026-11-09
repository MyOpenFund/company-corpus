"""``filings`` rows: what each report filing is, from the EDGAR submissions archive.

One row per report filing (``Config.store_report_forms``): its form and the period it reports
on (``reportDate``), as EDGAR lists them; ``valid_date`` is the filing date. Every column is a
function of the archive alone, so a replay re-emits the same rows (my-open-data chantier 2).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Collection
from datetime import date, datetime

from ..config import Config, normalize_cik
from .fundamentals import SOURCE_CODE, FundamentalsResult, KnownAtClock, source_doc_id
from .submissions import SubmissionsArchive, acceptance_times, filing_blocks

_FIELDS = ("form", "reportDate", "filingDate", "acceptanceDateTime")


def _iso_day(raw) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _listed(archive: SubmissionsArchive) -> tuple[dict[str, tuple], set[str]]:
    """accession -> its fields over the main page and every history page; the accessions
    listed twice with different fields."""
    seen: dict[str, tuple] = {}
    ambiguous: set[str] = set()
    for block in filing_blocks(archive):
        columns = [block.get(f) or [] for f in _FIELDS]
        for i, accn in enumerate(block.get("accessionNumber") or []):
            if not accn:
                continue
            fields = tuple((c[i] or None) if i < len(c) else None for c in columns)
            if seen.setdefault(accn, fields) != fields:
                ambiguous.add(accn)
    return seen, ambiguous


def filings_rows(cik: str, archive: SubmissionsArchive, *, manifest_doc_ids: Collection[str],
                 config: Config, now: datetime) -> FundamentalsResult:
    """``filings`` rows of one issuer. ``known_at`` follows the rule of the filing's facts."""
    clock = KnownAtClock.of(config, now)
    cik, counters = normalize_cik(cik), Counter()
    acceptance = acceptance_times(archive)
    report_forms = set(config.store_report_forms)
    listed, ambiguous = _listed(archive)
    rows = []
    for accn in sorted(listed):
        raw_form, raw_report, raw_filed, _ = listed[accn]
        if accn in ambiguous:
            counters["ambiguous_filing"] += 1
            continue
        form = raw_form.strip().upper() if isinstance(raw_form, str) else ""
        if form not in report_forms:
            counters["dropped:not_a_report"] += 1
            continue
        filed = _iso_day(raw_filed)
        if filed is None:
            counters["dropped:bad_filing_date"] += 1
            continue
        known_at, fell_back = clock.resolve(accn, filed, acceptance, counters)
        if known_at is None:
            continue
        if fell_back:
            counters["known_at_fallback"] += 1
        report = _iso_day(raw_report)
        if report is None:
            counters["report_date_missing"] += 1
        rows.append({
            "entity_scheme": "cik", "entity_value": cik, "filing_ref": accn, "form": form,
            "report_date": report, "valid_date": filed, "known_at": known_at,
            "known_at_kind": "published", "source_code": SOURCE_CODE,
            "source_doc_id": source_doc_id(cik, form, accn, manifest_doc_ids, counters)})
    return FundamentalsResult(rows=rows, counters=counters)
