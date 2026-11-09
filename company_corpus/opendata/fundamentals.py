"""SEC companyfacts points -> my-open-data ``fundamentals`` 3.0.0 rows, as filed.

Every observation of every dictionary tag is emitted, one row per filing that reports a value;
the my-open-data publisher skips what it already holds. Which tag answers which concept is decided
when reading (``concepts.selection``), so changing the dictionary changes no published row.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

from ..config import DEFAULT_REPORT_FORMS, Config, normalize_cik
from ..concepts.dictionary import HELPER_NAMESPACES, Dictionary, TagSpec, load_dictionary
from ..financials import _CURRENCY_RE
from ..models import FilingRecord
from ..taxonomy import from_edgar_form

#: Duration labels that are my-open-data frequencies; any other band label is a drop reason.
EMITTED_FREQUENCIES: tuple[str, ...] = ("quarterly", "semiannual", "annual")


@dataclass(frozen=True)
class Point:
    tag: str
    ns: str
    start: date | None
    end: date
    frequency: str     # quarterly | semiannual | annual | instant
    value: Decimal
    unit: str
    accn: str
    form: str
    filed: date | None


def ingest_namespaces(d: Dictionary) -> tuple[str, ...]:
    """The loaded tables' taxonomy namespaces, then the helper ones (cover page)."""
    return (*d.namespaces, *(ns for ns in HELPER_NAMESPACES if ns not in d.namespaces))


def flatten_with_namespace(facts: dict, namespaces: Sequence[str]) -> dict[str, list[dict]]:
    """``{"ns:Tag": [point + unit, tag, ns]}`` over ``namespaces``; no namespace hides another."""
    out: dict[str, list[dict]] = {}
    source = facts.get("facts") or {}
    for ns in namespaces:
        for tag, body in (source.get(ns) or {}).items():
            if not isinstance(body, dict):
                continue
            points = [{**p, "unit": unit, "tag": tag, "ns": ns}
                      for unit, arr in (body.get("units") or {}).items()
                      for p in arr or [] if isinstance(p, dict)]
            if points:
                out[f"{ns}:{tag}"] = points
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


def _unit_drop(spec: TagSpec, unit: str, currency: str | None) -> str | None:
    """Why a point's unit disqualifies it, or None. Monetary and per-share values keep only
    the filing's reporting currency (a convenience translation is not a second fact). The shape
    of the unit is checked first, so a malformed unit is ``unexpected_unit`` whatever the
    filing's currency signals say."""
    if spec.kind == "shares":
        return None if unit == "shares" else "unexpected_unit"
    if spec.kind == "money/shares":
        numerator = unit[:-len("/shares")] if unit.endswith("/shares") else ""
    else:
        numerator = unit
    if not _CURRENCY_RE.match(numerator):
        return "unexpected_unit"
    if currency == AMBIGUOUS_CURRENCY:
        return "currency_ambiguous"
    if currency is not None and numerator != currency:
        return "non_reporting_currency"
    return None


def _point(spec: TagSpec, p: dict, currency: str | None, bands, counters: Counter,
           frequencies: Collection[str]) -> Point | None:
    end, raw_start = _day(p.get("end")), p.get("start")
    start = _day(raw_start) if raw_start else None
    value, accn = _decimal(p.get("val")), p.get("accn") or ""
    form = (p.get("form") or "").strip()
    reason = _unit_drop(spec, p.get("unit") or "", currency)
    if reason:
        counters[f"dropped:{reason}"] += 1
        return None
    if end is None or (raw_start and start is None) or value is None or not accn:
        counters["dropped:bad_point"] += 1
        return None
    if _quantized(value) is None:
        counters["dropped:value_out_of_range"] += 1
        return None
    if (start is None) != spec.instant:
        counters["dropped:period_shape"] += 1
        return None
    if start is None:
        frequency = "instant"
    else:
        label = classify_duration((end - start).days + 1, bands)
        if label is None:
            counters["dropped:odd_duration"] += 1
            return None
        if label not in frequencies:
            counters[f"dropped:{label}"] += 1
            return None
        frequency = label
    return Point(p["tag"], p["ns"], start, end, frequency, value, p["unit"], accn, form,
                 _day(p.get("filed")))


def _unique_top(counts: Counter) -> str | None:
    """The most frequent key, or None when empty or tied."""
    top = counts.most_common(2)
    if not top or (len(top) == 2 and top[0][1] == top[1][1]):
        return None
    return top[0][0]


def _filing_currencies(flat: dict[str, list[dict]], anchor_tags: Collection[str],
                       per_share_tags: Collection[str],
                       voting_namespaces: Collection[str]) -> dict[str, str]:
    """Reporting currency of each filing, from that filing's own facts in the taxonomy namespaces only (not ``dei``): a dei value
    such as the public float can be in another currency, and an issuer-wide count would change
    as filings accumulate, so that a published row could change, which the publisher quarantines.

    Two signals: the vote over the filing's monetary facts, and the currency of its balance
    sheet totals (``anchor_tags``). A convenience translation loses both, since it usually covers
    the latest year only. They must agree when both exist: a tied vote is settled by the balance sheet, and when
    nothing settles it, or they contradict each other, the filing is ``AMBIGUOUS_CURRENCY`` and
    publishes no money. A filing without balance sheet totals (a partial 10-K/A) rests on the vote
    alone. A filing with no monetary fact at all but per-share points in more than one currency is
    ambiguous too, since nothing says which is the reporting one. Any three-letter unit such as ``BOE`` votes; this fails safe, since a stray unit can
    only produce a tie or a contradiction, which hold the money back. A row held back can still be published later; a wrong one cannot be
    corrected in an append-only store."""
    votes: dict[str, Counter] = defaultdict(Counter)
    anchors: dict[str, Counter] = defaultdict(Counter)
    per_share: dict[str, set[str]] = defaultdict(set)
    for full, points in flat.items():
        for p in points:
            unit = p.get("unit") or ""
            if full in per_share_tags and unit.endswith("/shares"):
                numerator = unit[:-len("/shares")]
                if _CURRENCY_RE.match(numerator):
                    per_share[p.get("accn") or ""].add(numerator)
            if p.get("ns") in voting_namespaces and _CURRENCY_RE.match(unit):
                accn = p.get("accn") or ""
                votes[accn][unit] += 1
                if full in anchor_tags:
                    anchors[accn][unit] += 1
    out = {}
    for accn, numerators in per_share.items():
        if accn not in votes and len(numerators) > 1:
            out[accn] = AMBIGUOUS_CURRENCY     # per-share points only, in several currencies
    for accn, vote in votes.items():
        by_vote, by_anchor = _unique_top(vote), _unique_top(anchors[accn])
        if by_vote and by_anchor and by_vote != by_anchor:
            out[accn] = AMBIGUOUS_CURRENCY
        else:
            out[accn] = by_vote or by_anchor or AMBIGUOUS_CURRENCY
    return out


def as_filed_points(flat: dict[str, list[dict]], bands, counters: Counter,
                    anchor_tags: Collection[str], *, dictionary: Dictionary | None = None,
                    frequencies: Collection[str] = EMITTED_FREQUENCIES,
                    report_forms: Collection[str] = DEFAULT_REPORT_FORMS) -> list[Point]:
    """Every usable point of every dictionary tag from a financial report, helpers included,
    whatever its priority."""
    d = dictionary or load_dictionary()
    specs = d.tag_specs()
    per_share = {full for full, spec in specs.items() if spec.kind == "money/shares"}
    currencies = _filing_currencies(flat, anchor_tags, per_share, d.namespaces)
    out: list[Point] = []
    for full, spec in specs.items():
        for p in flat.get(full, []):
            if (p.get("form") or "").strip().upper() not in report_forms:
                counters["dropped:not_a_report"] += 1
                continue
            pt = _point(spec, p, currencies.get(p.get("accn") or ""), bands, counters, frequencies)
            if pt is not None:
                out.append(pt)
    return out


SOURCE_CODE = "sec"
VALUE_QUANTUM = Decimal("0.000001")  # fundamentals.value is decimal(38, 6)


@dataclass
class FundamentalsResult:
    rows: list[dict]
    counters: Counter = field(default_factory=Counter)


@dataclass(frozen=True)
class KnownAtClock:
    """known_at of a filing: EDGAR acceptance time, else its filing date at the configured
    end-of-day time, capped at ``now``."""

    now: datetime
    tz: ZoneInfo
    at: time
    latest_filed: date

    @classmethod
    def of(cls, config: Config, now: datetime) -> KnownAtClock:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        latest = (now.astimezone(ZoneInfo(config.store_edgar_day_tz)).date()
                  + timedelta(days=config.store_stale_filed_max_days_ahead))
        return cls(now, ZoneInfo(config.store_known_at_fallback_tz),
                   time.fromisoformat(config.store_known_at_fallback_time), latest)

    def resolve(self, accn: str, filed: date | None, acceptance: Mapping[str, datetime],
                counters: Counter) -> tuple[datetime | None, bool]:
        """``(known_at, fell_back)``; known_at None when the observation must be dropped."""
        known_at = acceptance.get(accn)
        if known_at is not None and known_at.tzinfo is None:
            counters["known_at_naive"] += 1   # unusable: same path as a missing acceptance
            known_at = None
        if known_at is not None:
            return known_at, False
        if filed is None:
            counters["dropped:no_known_at"] += 1
            return None, False
        if filed > self.latest_filed:
            # A typo in the feed: capped at ``now`` it would be a new vintage every run.
            counters["dropped:implausible_filed"] += 1
            return None, False
        fallback = datetime.combine(filed, self.at, tzinfo=self.tz).astimezone(timezone.utc)
        if fallback > self.now:
            counters["known_at_capped"] += 1
        return min(fallback, self.now), True


def fundamentals_rows(cik: str, facts: dict, *, acceptance: Mapping[str, datetime],
                      manifest_doc_ids: Collection[str], config: Config, now: datetime,
                      dictionary: Dictionary | None = None) -> FundamentalsResult:
    """One row per (tag, period, frequency, known_at) the issuer's filings report.

    Two filings accepted the same second that report the same key: equal values keep one row
    (lowest accession), different values emit none (``ambiguous_observation``).

    The history is re-emitted every run and the store refuses a row whose (natural key,
    known_at) it holds with another value, so every column must be a pure function of immutable
    inputs. ``fiscal_year`` is always null: it can only be derived from the year-ends known
    today, which change for already-published rows; a reader view will derive it.
    """
    clock = KnownAtClock.of(config, now)
    cik, counters = normalize_cik(cik), Counter()
    d = dictionary or load_dictionary()
    points = as_filed_points(flatten_with_namespace(facts, ingest_namespaces(d)),
                             config.store_duration_bands, counters,
                             config.store_currency_anchor_tags, dictionary=d,
                             report_forms=config.store_report_forms)
    by_key: dict[tuple, list[tuple[Point, bool]]] = defaultdict(list)
    for pt in points:
        known_at, fell_back = clock.resolve(pt.accn, pt.filed, acceptance, counters)
        if known_at is not None:
            by_key[(pt.ns, pt.tag, pt.end, pt.frequency, known_at)].append((pt, fell_back))
    rows = []
    for (_, _, _, _, known_at), cands in by_key.items():
        if len({_quantized(pt.value) for pt, _ in cands}) > 1:
            counters["ambiguous_observation"] += 1
            continue
        pt, fell_back = min(cands, key=lambda c: (c[0].accn, c[0].unit, c[0].value,
                                                  c[0].start is not None, c[0].start or date.min,
                                                  c[0].form))
        if fell_back:
            counters["known_at_fallback"] += 1
        rows.append(_row(cik, pt, known_at, manifest_doc_ids, counters))
    return FundamentalsResult(rows=rows, counters=counters)


def source_doc_id(cik: str, form: str, accn: str, manifest_doc_ids, counters: Counter) -> str | None:
    """The filing's doc_id (S2): its family from the form without "/A" (10-K/A -> A1)."""
    upper = form.upper()
    ft = from_edgar_form(upper[:-2] if upper.endswith("/A") else upper) if upper else None
    if ft is None:
        counters["source_doc_id_null"] += 1
        return None
    doc_id = FilingRecord(cik=cik, form_type=ft, sec_form=form, accession=accn).doc_id
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
        "entity_scheme": "cik", "entity_value": cik,
        "original_tag": f"{pt.ns}:{pt.tag}", "period_start": pt.start,
        "frequency": pt.frequency, "fiscal_year": None, "basis": "consolidated",
        "value": value, "unit": pt.unit, "extraction_method": "xbrl_filed",
        "confidence": None, "source_page": None, "filing_ref": pt.accn, "valid_date": pt.end,
        "known_at": known_at, "known_at_kind": "published", "source_code": SOURCE_CODE,
        "source_doc_id": source_doc_id(cik, pt.form, pt.accn, manifest_doc_ids, counters),
    }
