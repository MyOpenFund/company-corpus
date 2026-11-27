"""SEC companyfacts -> F1 period summaries, through the selection the store's readers use (spec §8)."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pyarrow as pa

from ..config import DEFAULT_CURRENCY_ANCHOR_TAGS
from ..financials import (
    FlowSeries,
    PeriodSummary,
    Q4Rule,
    _attach_ttm,
    _TTM_FLOW_KEYS,
    reporting_currency,
)
from ..opendata.fundamentals import (
    Point,
    _quantized,
    as_filed_points,
    flatten_with_namespace,
    ingest_namespaces,
)
from .coverage import coverage, positioned_tags
from .dictionary import Dictionary, load_dictionary
from .selection import run_selection

#: The F1 engine's own bands, in days = end - start + 1 (its historical 80-100, 160-200, 250-290
#: and 330-400 on end - start). Wider than the store's: a 10-KT transition stays an annual summary.
F1_DURATION_BANDS: tuple[tuple[str, int, int], ...] = (
    ("quarterly", 81, 101),
    ("semiannual", 161, 201),
    ("ytd9", 251, 291),
    ("annual", 331, 401),
)
F1_FREQUENCIES = ("quarterly", "semiannual", "ytd9", "annual")
#: F1 says "semi-annual" in its rows and doc ids.
_F1_LABEL = {"semiannual": "semi-annual"}
#: F1 has no acceptance time at hand: filings order by filing date, at the end of that day.
_END_OF_DAY = time(23, 59, 59)

FACT_SCHEMA = pa.schema([
    ("entity_scheme", pa.string()), ("entity_value", pa.string()), ("original_tag", pa.string()),
    ("period_start", pa.date32()), ("frequency", pa.string()), ("basis", pa.string()),
    ("value", pa.decimal128(38, 6)), ("unit", pa.string()), ("filing_ref", pa.string()),
    ("source_doc_id", pa.string()), ("known_at", pa.timestamp("us", tz="UTC")),
    ("valid_date", pa.date32()), ("source_code", pa.string())])
POSITION_SCHEMA = pa.schema([
    ("entity_scheme", pa.string()), ("entity_value", pa.string()), ("filing_ref", pa.string()),
    ("statement", pa.string()), ("tag", pa.string()), ("tag_version", pa.string()),
    ("known_at", pa.timestamp("us", tz="UTC"))])


@dataclass
class Selected:
    rows: list[dict]
    points: list[Point]
    counters: Counter = field(default_factory=Counter)


def _end_of_day(day: date) -> datetime:
    return datetime.combine(day, _END_OF_DAY, tzinfo=timezone.utc)


def _filed(raw: str) -> date | None:
    """``YYYYMMDD`` as in the bulk files."""
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except (TypeError, ValueError):
        return None


def facts_table(points: list[Point]) -> pa.Table:
    return pa.Table.from_pylist([
        {"entity_scheme": "cik", "entity_value": "f1", "original_tag": f"{p.ns}:{p.tag}",
         "period_start": p.start, "frequency": p.frequency, "basis": "consolidated",
         "value": _quantized(p.value), "unit": p.unit, "filing_ref": p.accn, "source_doc_id": None,
         "known_at": _end_of_day(p.filed), "valid_date": p.end, "source_code": "sec"}
        for p in points], schema=FACT_SCHEMA)


def positions_table(statements: Sequence[dict]) -> pa.Table:
    rows = []
    for s in statements:
        day = _filed(s.get("filed"))
        if day is not None:
            rows.append({"entity_scheme": "cik", "entity_value": "f1", "filing_ref": s["accn"],
                         "statement": s["statement"], "tag": s["tag"],
                         "tag_version": s.get("version") or "", "known_at": _end_of_day(day)})
    return pa.Table.from_pylist(rows, schema=POSITION_SCHEMA)


def select_for_f1(facts: dict, statements: Sequence[dict] = (),
                  dictionary: Dictionary | None = None) -> Selected:
    """Selection over one issuer's companyfacts and statement positions, in the F1 bands, with
    F1's issuer-wide reporting currency for monetary values."""
    d = dictionary or load_dictionary()
    flat = flatten_with_namespace(facts, ingest_namespaces(d))
    currency, counters = reporting_currency(flat), Counter()
    specs = d.tag_specs()
    points = [p for p in as_filed_points(flat, F1_DURATION_BANDS, counters,
                                         DEFAULT_CURRENCY_ANCHOR_TAGS, dictionary=d,
                                         frequencies=F1_FREQUENCIES)
              if p.filed is not None and _quantized(p.value) is not None
              and (specs[f"{p.ns}:{p.tag}"].kind != "money" or currency is None or p.unit == currency)]
    return Selected(run_selection(facts_table(points), positions_table(statements), d), points,
                    counters)


def _plain(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _value(row: dict, d: Dictionary, namespaces: Sequence[str]) -> dict:
    """One summary value. A derived quarter (chantier 12) reports its later input's filed tag,
    without the ``derived:`` prefix (the formula is in ``derived``): the engine compares tags by
    equality (e.g. the mixed equity base gate of ``compute_derived``)."""
    tag = row["original_tag"].removeprefix("derived:")
    for ns in namespaces:
        tag = tag.replace(f"{ns}:", "")
    return {"value": _plain(row["value"]), "unit": row["unit"],
            "label": d.concepts[row["concept"]].label, "tag": tag, "location": row["location"],
            "conflict": row["conflict"], "on_statement": row["on_statement"],
            "derived": row.get("derived")}


def group_periods(rows: Iterable[dict]) -> tuple[dict[tuple[date, str], dict[str, dict]],
                                                 dict[date, dict[str, dict]], set[str]]:
    """Selection rows grouped by period: duration rows by ``(valid_date, frequency)`` (ytd9
    excluded), instant rows by date, and the duration frequencies that get a summary (annual,
    plus quarterly, or semiannual when the issuer has no quarters). Shared by
    :func:`build_summaries` and the store readers'
    :func:`~company_corpus.concepts.store_summaries.summaries_from_rows`."""
    duration: dict[tuple[date, str], dict[str, dict]] = {}
    instant: dict[date, dict[str, dict]] = {}
    for r in rows:
        if r["frequency"] == "instant":
            instant.setdefault(r["valid_date"], {})[r["concept"]] = r
        elif r["frequency"] != "ytd9":
            duration.setdefault((r["valid_date"], r["frequency"]), {})[r["concept"]] = r
    seen = {freq for _, freq in duration}
    allowed = {"annual"} | ({"quarterly"} if "quarterly" in seen else
                            {"semiannual"} if "semiannual" in seen else set())
    return duration, instant, allowed


def with_quarters(rows: Iterable[dict], dictionary: Dictionary | None = None) -> list[dict]:
    """Selection rows plus their derived quarters (chantier 12), as both summary paths group
    them: :func:`build_summaries` here and the store readers'
    :func:`~company_corpus.concepts.store_summaries.summaries_from_rows`."""
    from .quarters import with_derived_quarters  # quarters imports this module's bands

    return with_derived_quarters(rows, dictionary)


def ttm_rows(rows: Iterable[dict]) -> list[dict]:
    """The rows TTM reads: every row but the derived Q4s (spec rule 7). The Q4 rule (``ytd9`` or
    ``sum_of_quarters``) keeps deciding the fiscal year-end quarter; derived Q2/Q3 are ordinary
    quarters to it."""
    from .quarters import FORMULA_Q4

    return [r for r in rows if r.get("derived") != FORMULA_Q4]


def _flow_series(rows: Iterable[dict]) -> FlowSeries:
    """The TTM flow concepts of selection rows, by band (quarterly, annual, ytd9) and period end."""
    buckets: dict[str, dict[str, dict[date, float]]] = {"quarterly": {}, "annual": {}, "ytd9": {}}
    for r in rows:
        if r["concept"] in _TTM_FLOW_KEYS and r["frequency"] in buckets:
            buckets[r["frequency"]].setdefault(r["concept"], {})[r["valid_date"]] = _plain(r["value"])
    return FlowSeries(**buckets)


def build_summaries(facts: dict, *, company: str, company_current: str, name_for_date=None,
                    since_year: int | None = None, until_year: int | None = None,
                    sic: str | None = None, statements: Sequence[dict] = (),
                    dictionary: Dictionary | None = None) -> list[PeriodSummary]:
    """One summary per reporting period as the company reports it: annual, plus quarterly, or
    semi-annual when the issuer has no quarters. The publication date is the earliest filing of
    any of the period's facts made once the period had ended: a filing can already hold a fact about
    a later period, such as a declared dividend. A period only seen before it ended has no summary.
    The values are the selection's, plus the derived quarters (chantier 12): a derived row is
    a publication-date candidate of its period too (its later input's filing), so a derived Q4
    gets a quarterly summary dated and accessioned by the 10-K."""
    d = dictionary or load_dictionary()
    selected = select_for_f1(facts, statements, d)
    specs = d.tag_specs()
    namespaces = sorted({s.ns for s in specs.values()})
    chain_tags = {full for t in d.tables for e in t.entries.values() for full in e.all_tags}
    duration, instant, allowed = group_periods(with_quarters(selected.rows, d))
    points_at: dict[tuple[date, str], list[tuple[date, str, str | None]]] = {}
    forms: dict[str, str | None] = {}
    for p in selected.points:
        forms.setdefault(p.accn, p.form)
        if f"{p.ns}:{p.tag}" in chain_tags:
            points_at.setdefault((p.end, p.frequency), []).append((p.filed, p.accn, p.form))
    summaries: list[PeriodSummary] = []
    for (end, freq), per_concept in duration.items():
        if freq not in allowed:
            continue
        if since_year is not None and end.year < since_year:
            continue
        if until_year is not None and end.year > until_year:
            continue
        merged = {**per_concept, **instant.get(end, {})}
        values = {key: _value(merged[key], d, namespaces) for key in d.concepts if key in merged}
        # Filed points first: on a tie the earliest filed point wins, as before chantier 12.
        derived = [(r["known_at"].date(), r["filing_ref"], forms.get(r["filing_ref"]))
                   for r in per_concept.values() if r.get("derived") is not None]
        reported = [c for c in (*points_at.get((end, freq), ()), *points_at.get((end, "instant"), ()),
                                *derived) if c[0] >= end]
        if not reported:
            continue
        filed, accn, form = min(reported, key=lambda c: c[0])
        pit = name_for_date(filed) if name_for_date else company
        summaries.append(PeriodSummary(
            period_end=end, frequency=_F1_LABEL.get(freq, freq), publication_date=filed,
            sec_form=form or "XBRL", accession=accn, company=pit or company,
            company_current=company_current, values=values, currency=_currency(facts, d), sic=sic,
            debt_rule="concepts"))
    summaries.sort(key=lambda s: (s.period_end or date.min, s.frequency), reverse=True)
    return summaries


def _currency(facts: dict, d: Dictionary) -> str:
    return reporting_currency(flatten_with_namespace(facts, ingest_namespaces(d))) or "USD"


def attach_ttm(facts: dict, summaries: list[PeriodSummary], *, statements: Sequence[dict] = (),
               dictionary: Dictionary | None = None, q4_rule: Q4Rule = "ytd9") -> None:
    """TTM metrics on F1 ``summaries``, from the selection plus its derived Q2/Q3 (spec rule 7:
    a quarterly TTM at Q2/Q3 of a year-to-date filer becomes computable)."""
    rows = with_quarters(select_for_f1(facts, statements, dictionary).rows, dictionary)
    _attach_ttm(_flow_series(ttm_rows(rows)), summaries, ttm_keys=_TTM_FLOW_KEYS, q4_rule=q4_rule)


def f1_coverage(facts: dict, statements: Sequence[dict] = (),
                dictionary: Dictionary | None = None) -> Counter:
    d = dictionary or load_dictionary()
    on_statement = positioned_tags(((s["accn"], s["tag"], s.get("version") or "")
                                    for s in statements), d)
    return coverage(select_for_f1(facts, statements, d).rows, on_statement, d)
