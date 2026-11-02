"""SEC companyfacts points -> my-open-data ``fundamentals`` 2.0.0 rows (spec §4, S1-S9).

Raw points, not the F1 summaries: the F1 engine keeps only the latest-filed value per
period (``financials._latest_filed``), so restatements and comparatives would be lost. Every
observation is emitted, one row per filing that reports a value (S4); the my-open-data
publisher skips what it already holds (S3).
"""
from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

from ..config import Config, normalize_cik
from ..financials import _CURRENCY_RE, CONCEPTS, Concept, reporting_currency
from ..models import FilingRecord
from ..taxonomy import from_edgar_form

#: Namespaces read from companyfacts. ``dei`` is needed because one curated chain falls
#: back to a cover-page tag (shares_outstanding -> dei:EntityCommonStockSharesOutstanding).
NAMESPACES: tuple[str, ...] = ("us-gaap", "dei")
#: Duration labels that are my-open-data frequencies; any other band label is a drop reason.
EMITTED_FREQUENCIES: tuple[str, ...] = ("quarterly", "semiannual", "annual")


@dataclass(frozen=True)
class Point:
    concept: str
    tag: str
    ns: str
    prio: int          # position of the tag in the concept's priority chain
    start: date | None
    end: date
    frequency: str     # quarterly | semiannual | annual | instant
    value: Decimal
    unit: str
    accn: str
    form: str
    filed: date | None


def flatten_with_namespace(facts: dict) -> dict[str, list[dict]]:
    """``{tag: [point + unit, tag, ns]}``; a tag present in two namespaces keeps us-gaap's."""
    out: dict[str, list[dict]] = {}
    namespaces = facts.get("facts") or {}
    for ns in NAMESPACES:
        for tag, body in (namespaces.get(ns) or {}).items():
            if tag in out or not isinstance(body, dict):
                continue
            points = [{**p, "unit": unit, "tag": tag, "ns": ns}
                      for unit, arr in (body.get("units") or {}).items()
                      for p in arr or [] if isinstance(p, dict)]
            if points:
                out[tag] = points
    return out


def classify_duration(days: int, bands) -> str | None:
    for label, lo, hi in bands:
        if lo <= days <= hi:
            return label
    return None


def _day(value) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _unit_drop(concept: Concept, unit: str, currency: str | None) -> str | None:
    """Why a point's unit disqualifies it, or None. Monetary and per-share values keep only
    the issuer's reporting currency (a convenience translation is not a second fact)."""
    if concept.unit == "shares":
        return None if unit == "shares" else "unexpected_unit"
    if concept.unit.endswith("/shares"):
        if not unit.endswith("/shares"):
            return "unexpected_unit"
        if currency is not None and unit != f"{currency}/shares":
            return "non_reporting_currency"
        return None
    if not _CURRENCY_RE.match(unit):
        return "unexpected_unit"
    if currency is not None and unit != currency:
        return "non_reporting_currency"
    return None


def _point(concept: Concept, prio: int, p: dict, currency: str | None, bands,
           counters: Counter, transition_ends: dict[str, date] | None) -> Point | None:
    end, raw_start = _day(p.get("end")), p.get("start")
    start = _day(raw_start) if raw_start else None
    value, accn = _decimal(p.get("val")), p.get("accn") or ""
    form = (p.get("form") or "").strip()
    if (end is not None and not (raw_start and start is None) and accn
            and start is not None and transition_ends is not None
            and form.upper().startswith("10-KT")):
        # Recorded before any drop (unit, shape, duration): the transition period ends on the
        # new fiscal year-end, the latest duration end a 10-KT reports (its comparatives and
        # quarters end earlier).
        transition_ends[accn] = max(end, transition_ends.get(accn, end))
    reason = _unit_drop(concept, p.get("unit") or "", currency)
    if reason:
        counters[f"dropped:{reason}"] += 1
        return None
    if end is None or (raw_start and start is None) or value is None or not accn:
        counters["dropped:bad_point"] += 1
        return None
    if (start is None) != concept.instant:
        counters["dropped:period_shape"] += 1
        return None
    if start is None:
        frequency = "instant"
    else:
        label = classify_duration((end - start).days + 1, bands)
        if label is None:
            counters["dropped:odd_duration"] += 1
            return None
        if label not in EMITTED_FREQUENCIES:
            counters[f"dropped:{label}"] += 1
            return None
        frequency = label
    return Point(concept.key, p["tag"], p["ns"], prio, start, end, frequency, value,
                 p["unit"], accn, form, _day(p.get("filed")))


def select_points(flat: dict[str, list[dict]], bands, counters: Counter,
                  transition_ends: dict[str, date] | None = None) -> list[Point]:
    """Every usable point of the curated concepts; per (concept, period end, frequency,
    filing) only the points of the highest-priority tag present (spec S6).
    ``transition_ends`` (optional, filled in place): 10-KT accession -> transition end.
    ``shares_outstanding`` may yield both the balance-sheet-date value
    (``CommonStockSharesOutstanding``) and the cover-date value
    (``dei:EntityCommonStockSharesOutstanding``) of the same filing: their valid dates differ."""
    currency = reporting_currency(flat)
    groups: dict[tuple, list[Point]] = defaultdict(list)
    for concept in CONCEPTS:
        for prio, tag in enumerate(concept.tags):
            for p in flat.get(tag, []):
                pt = _point(concept, prio, p, currency, bands, counters, transition_ends)
                if pt is not None:
                    groups[(pt.concept, pt.end, pt.frequency, pt.accn)].append(pt)
    out: list[Point] = []
    for pts in groups.values():
        best = min(p.prio for p in pts)
        out.extend(p for p in pts if p.prio == best)
    return out


SOURCE_CODE = "sec"
VALUE_QUANTUM = Decimal("0.000001")  # fundamentals.value is decimal(38, 6)


@dataclass
class FundamentalsResult:
    rows: list[dict]
    counters: Counter = field(default_factory=Counter)


def fundamentals_rows(cik: str, facts: dict, *, acceptance: Mapping[str, datetime],
                      manifest_doc_ids: Collection[str], config: Config,
                      now: datetime) -> FundamentalsResult:
    """One row per (concept, period, frequency, known_at) the issuer's filings report.

    known_at = EDGAR acceptance time of the filing (S9), else its filing date at the
    configured end-of-day time, capped at ``now``. Two filings accepted the same second that
    report the same key: equal values keep one row (lowest accession), different values emit
    none (``ambiguous_observation``).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cik, counters = normalize_cik(cik), Counter()
    transitions: dict[str, date] = {}
    points = select_points(flatten_with_namespace(facts), config.store_duration_bands, counters,
                           transitions)
    tz = ZoneInfo(config.store_known_at_fallback_tz)
    at = time.fromisoformat(config.store_known_at_fallback_time)
    by_key: dict[tuple, list[tuple[Point, bool]]] = defaultdict(list)
    for pt in points:
        known_at = acceptance.get(pt.accn)
        if known_at is not None and known_at.tzinfo is None:
            counters["known_at_naive"] += 1   # unusable: same path as a missing acceptance
            known_at = None
        if known_at is None:
            if pt.filed is None:
                counters["dropped:no_known_at"] += 1
                continue
            fallback = datetime.combine(pt.filed, at, tzinfo=tz).astimezone(timezone.utc)
            if fallback > now:
                counters["known_at_capped"] += 1
            by_key[(pt.concept, pt.end, pt.frequency, min(fallback, now))].append((pt, True))
        else:
            by_key[(pt.concept, pt.end, pt.frequency, known_at)].append((pt, False))
    rows, annual_ends = [], set()
    for (_, _, _, known_at), cands in by_key.items():
        if len({_quantized(pt.value) for pt, _ in cands}) > 1:
            counters["ambiguous_observation"] += 1
            continue
        pt, fell_back = min(cands, key=lambda c: (c[0].accn, c[0].prio, c[0].tag, c[0].value,
                                         c[0].start is not None, c[0].start or date.min))
        if fell_back:
            counters["known_at_fallback"] += 1
        rows.append(_row(cik, pt, known_at, manifest_doc_ids, counters))
        if pt.frequency == "annual" and pt.form.upper().startswith("10-K"):
            annual_ends.add(pt.end)
    # Fiscal year-ends: ends of annual periods of 10-K-family filings (a 12-months figure in
    # a 10-Q/8-K is not a year-end), plus the end of every transition period (10-KT), so a
    # fiscal-year change labels both sides right (spec §4, §9).
    fyes = sorted(annual_ends | set(transitions.values()))
    for r in rows:
        r["fiscal_year"] = fiscal_year_of(r["valid_date"], fyes,
                                          config.store_fiscal_year_slack_days)
    return FundamentalsResult(rows=rows, counters=counters)


def _source_doc_id(cik: str, pt: Point, manifest_doc_ids, counters: Counter) -> str | None:
    """The filing's doc_id (S2): its family from the form without "/A" (10-K/A -> A1)."""
    form = pt.form.upper()
    ft = from_edgar_form(form[:-2] if form.endswith("/A") else form) if form else None
    if ft is None:
        counters["source_doc_id_null"] += 1
        return None
    doc_id = FilingRecord(cik=cik, form_type=ft, sec_form=pt.form, accession=pt.accn).doc_id
    if doc_id not in manifest_doc_ids:
        counters["filing_not_in_manifest"] += 1
    return doc_id


def _quantized(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 38
        return value.quantize(VALUE_QUANTUM)


def _row(cik: str, pt: Point, known_at: datetime, manifest_doc_ids,
         counters: Counter) -> dict:
    value = _quantized(pt.value)
    if value != pt.value:
        counters["value_rounded"] += 1
    return {
        "entity_scheme": "cik", "entity_value": cik, "concept": pt.concept,
        "original_tag": f"{pt.ns}:{pt.tag}", "period_start": pt.start,
        "frequency": pt.frequency, "fiscal_year": None, "basis": "consolidated",
        "value": value, "unit": pt.unit, "extraction_method": "xbrl_filed",
        "confidence": None, "source_page": None, "valid_date": pt.end,
        "known_at": known_at, "known_at_kind": "published", "source_code": SOURCE_CODE,
        "source_doc_id": _source_doc_id(cik, pt, manifest_doc_ids, counters),
    }


def fiscal_year_of(valid_date: date, fiscal_year_ends: list[date],
                   slack_days: int) -> int | None:
    """Year of the first fiscal year-end on or after ``valid_date`` (not companyfacts ``fy``,
    which is the filing's fiscal year). After the last known year-end, that year-end is
    projected by whole years; a period ending within ``slack_days`` after a projected
    year-end belongs to it (52/53-week drift). None when the issuer reports no annual period.
    """
    if not fiscal_year_ends:
        return None
    i = bisect.bisect_left(fiscal_year_ends, valid_date)
    slack = timedelta(days=slack_days)
    if i == 0 and valid_date < fiscal_year_ends[0]:
        # Before the first known year-end (an opening balance): project it backwards.
        d = fiscal_year_ends[0]
        while True:
            try:
                prev = d.replace(year=d.year - 1)
            except ValueError:  # 29 February
                prev = d.replace(year=d.year - 1, day=28)
            if prev + slack < valid_date:
                return d.year
            d = prev
    if i < len(fiscal_year_ends):
        return fiscal_year_ends[i].year
    d = fiscal_year_ends[-1]
    while d + slack < valid_date:
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:  # 29 February
            d = d.replace(year=d.year + 1, day=28)
    return d.year
