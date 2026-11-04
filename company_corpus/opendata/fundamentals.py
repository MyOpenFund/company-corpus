"""SEC companyfacts points -> my-open-data ``fundamentals`` 2.0.0 rows (spec §4, S1-S9).

Raw points, not the F1 summaries: the F1 engine keeps only the latest-filed value per
period (``financials._latest_filed``), so restatements and comparatives would be lost. Every
observation is emitted, one row per filing that reports a value (S4); the my-open-data
publisher skips what it already holds (S3).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

from ..config import DEFAULT_CURRENCY_ANCHOR_TAGS, Config, normalize_cik
from ..financials import _CURRENCY_RE, CONCEPTS, Concept
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


AMBIGUOUS_CURRENCY = "?"  # a filing whose currency signals do not settle; no ISO unit matches


def _unit_drop(concept: Concept, unit: str, currency: str | None) -> str | None:
    """Why a point's unit disqualifies it, or None. Monetary and per-share values keep only
    the filing's reporting currency (a convenience translation is not a second fact)."""
    if concept.unit == "shares":
        return None if unit == "shares" else "unexpected_unit"
    if currency == AMBIGUOUS_CURRENCY:
        return "currency_ambiguous"
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
           counters: Counter) -> Point | None:
    end, raw_start = _day(p.get("end")), p.get("start")
    start = _day(raw_start) if raw_start else None
    value, accn = _decimal(p.get("val")), p.get("accn") or ""
    form = (p.get("form") or "").strip()
    reason = _unit_drop(concept, p.get("unit") or "", currency)
    if reason:
        counters[f"dropped:{reason}"] += 1
        return None
    if end is None or (raw_start and start is None) or value is None or not accn:
        counters["dropped:bad_point"] += 1
        return None
    if _quantized(value) is None:
        counters["dropped:value_out_of_range"] += 1
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


def _unique_top(counts: Counter) -> str | None:
    """The most frequent key, or None when empty or tied."""
    top = counts.most_common(2)
    if not top or (len(top) == 2 and top[0][1] == top[1][1]):
        return None
    return top[0][0]


def _filing_currencies(flat: dict[str, list[dict]],
                       anchor_tags: Collection[str]) -> dict[str, str]:
    """Reporting currency of each filing, from that filing's own us-gaap facts only: a dei value
    such as the public float can be in another currency, and an issuer-wide count would change
    as filings accumulate, so that a published row could change, which the publisher quarantines.

    Two signals: the vote over the filing's monetary facts, and the currency of its balance
    sheet totals (``anchor_tags``). A convenience translation loses both, since it usually covers
    the latest year only. They must agree: a tied vote is settled by the balance sheet, and when
    nothing settles it, or they contradict each other, the filing is ``AMBIGUOUS_CURRENCY`` and
    publishes no money. A row held back can still be published later; a wrong one cannot be
    corrected in an append-only store."""
    votes: dict[str, Counter] = defaultdict(Counter)
    anchors: dict[str, Counter] = defaultdict(Counter)
    for tag, points in flat.items():
        for p in points:
            unit = p.get("unit") or ""
            if p.get("ns") == "us-gaap" and _CURRENCY_RE.match(unit):
                accn = p.get("accn") or ""
                votes[accn][unit] += 1
                if tag in anchor_tags:
                    anchors[accn][unit] += 1
    out = {}
    for accn, vote in votes.items():
        by_vote, by_anchor = _unique_top(vote), _unique_top(anchors[accn])
        if by_vote and by_anchor and by_vote != by_anchor:
            out[accn] = AMBIGUOUS_CURRENCY
        else:
            out[accn] = by_vote or by_anchor or AMBIGUOUS_CURRENCY
    return out


def select_points(flat: dict[str, list[dict]], bands, counters: Counter,
                  anchor_tags: Collection[str] = DEFAULT_CURRENCY_ANCHOR_TAGS) -> list[Point]:
    """Every usable point of the curated concepts; per (concept, period end, frequency,
    filing) only the points of the highest-priority tag present (spec S6).
    ``shares_outstanding`` may yield both the balance-sheet-date value
    (``CommonStockSharesOutstanding``) and the cover-date value
    (``dei:EntityCommonStockSharesOutstanding``) of the same filing: their valid dates differ."""
    currencies = _filing_currencies(flat, anchor_tags)
    groups: dict[tuple, list[Point]] = defaultdict(list)
    for concept in CONCEPTS:
        for prio, tag in enumerate(concept.tags):
            for p in flat.get(tag, []):
                pt = _point(concept, prio, p, currencies.get(p.get("accn") or ""), bands, counters)
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

    The history is re-emitted every run and the store refuses a row whose (natural key,
    known_at) it holds with another value, so every column must be a pure function of immutable
    inputs. ``fiscal_year`` is always null: it can only be derived from the year-ends known
    today, which change for already-published rows; a reader view will derive it.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cik, counters = normalize_cik(cik), Counter()
    points = select_points(flatten_with_namespace(facts), config.store_duration_bands, counters,
                           config.store_currency_anchor_tags)
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
    rows = []
    for (_, _, _, known_at), cands in by_key.items():
        if len({_quantized(pt.value) for pt, _ in cands}) > 1:
            counters["ambiguous_observation"] += 1
            continue
        pt, fell_back = min(cands, key=lambda c: (c[0].accn, c[0].prio, c[0].tag, c[0].unit, c[0].value,
                                         c[0].start is not None, c[0].start or date.min))
        if fell_back:
            counters["known_at_fallback"] += 1
        rows.append(_row(cik, pt, known_at, manifest_doc_ids, counters))
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


def _quantized(value: Decimal) -> Decimal | None:
    """``value`` at the contract's scale, or None when it does not fit decimal(38, 6)."""
    with localcontext() as ctx:
        ctx.prec = 38
        try:
            return value.quantize(VALUE_QUANTUM)
        except InvalidOperation:
            return None


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
