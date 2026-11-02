"""SEC companyfacts points -> my-open-data ``fundamentals`` 2.0.0 rows (spec §4, S1-S9).

Raw points, not the F1 summaries: the F1 engine keeps only the latest-filed value per
period (``financials._latest_filed``), so restatements and comparatives would be lost. Every
observation is emitted, one row per filing that reports a value (S4); the my-open-data
publisher skips what it already holds (S3).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from ..financials import _CURRENCY_RE, CONCEPTS, Concept, reporting_currency

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
