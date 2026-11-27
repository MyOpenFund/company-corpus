"""Standalone quarters derived from year-to-date rows (chantier 12).

An issuer that files its statements year to date (3, 6, 9 months, then the fiscal year) has a
filed quarter for Q1 only. :func:`with_derived_quarters` adds the others to a selection, as
ordinary ``quarterly`` rows marked derived: Q2 = 6M - Q1, Q3 = 9M - 6M, Q4 = FY - 9M.

Rules (spec §3): a filed quarter always wins; both inputs or nothing, never chained from a derived
row; only money flow concepts (``kind == "money"``, not instant: a per-share figure does not
subtract); a derived row is known when its later input is (its ``known_at``, ``filing_ref`` and
``source_doc_id``); a negative difference is kept. A derived span outside F1's quarterly band
is no quarter and is not produced (``quarters:not_a_quarter``). The store is never touched.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import date, timedelta

from company_corpus.concepts.dictionary import Dictionary, load_dictionary
from company_corpus.concepts.summaries import F1_DURATION_BANDS
from company_corpus.financials import PeriodSummary, compute_derived

__all__ = ["FORMULA_Q2", "FORMULA_Q3", "FORMULA_Q4", "PAIRS", "QUARTER_BAND",
           "hidden_semiannual", "metrics_from_derived", "with_derived_quarters"]

FORMULA_Q2 = "6M - Q1"
FORMULA_Q3 = "9M - 6M"
FORMULA_Q4 = "FY - 9M"
#: (earlier frequency, later frequency, formula): the quarter is later - earlier, ending at the
#: later row's ``valid_date`` and starting the day after the earlier row's.
PAIRS: tuple[tuple[str, str, str], ...] = (
    ("quarterly", "semiannual", FORMULA_Q2),
    ("semiannual", "ytd9", FORMULA_Q3),
    ("ytd9", "annual", FORMULA_Q4),
)
#: A derived span (days = end - start + 1) must fall in F1's quarterly band.
QUARTER_BAND: tuple[int, int] = next((lo, hi) for f, lo, hi in F1_DURATION_BANDS
                                     if f == "quarterly")
_LOCATION = "derived"
_TAG_PREFIX = "derived:"


def _eligible(d: Dictionary) -> set[str]:
    return {k for k, c in d.concepts.items() if c.kind == "money" and not c.instant}


def _quarter_key(r: dict) -> tuple:
    return (r["concept"], r["entity_scheme"], r["entity_value"], r["basis"], r["source_code"],
            r["valid_date"])


def with_derived_quarters(rows: Iterable[dict], dictionary: Dictionary | None = None, *,
                          counters: Counter | None = None) -> list[dict]:
    """``rows`` (selection rows of one issuer) unchanged and in order, then the derived quarters
    sorted by concept and ``valid_date``.

    Inputs pair when they share ``(concept, entity, basis, source_code, unit, period_start)``:
    the fiscal year's start. A quarter already present for the concept, entity, basis, source and
    ``valid_date`` (filed or derived by an earlier pass) is never derived again, so the function
    is idempotent. That check comes before the band: an out-of-band pair whose quarter is
    already there counts nothing in ``quarters:not_a_quarter``.

    A derived quarter is never an input (never chained): a row with a ``derived`` value, or whose
    ``original_tag`` starts with ``derived:`` (should a projection drop the ``derived`` key). A
    same-period derived *total* of the selection (``location`` derived, ``original_tag``
    ``computed:<tag>``) is a filed-period value and IS a valid input.

    A derived row copies its later input (so ``known_at``, ``filing_ref``, ``source_doc_id``,
    ``review_status`` are the later filing's), then sets ``frequency`` (quarterly),
    ``period_start``, ``value``, ``location`` (derived), ``original_tag`` (``derived:`` + the
    later input's) and ``derived`` (the formula). ``conflict`` is the OR of the inputs' and
    ``on_statement`` the AND: more truthful than the blanket FALSE/TRUE of the derived totals
    (spec §4 reuses their convention for ``location`` and ``original_tag`` only). Filed rows
    carry no ``derived`` key.
    """
    d = dictionary if dictionary is not None else load_dictionary()
    counters = counters if counters is not None else Counter()
    rows = list(rows)
    eligible = _eligible(d)
    have = {_quarter_key(r) for r in rows if r["frequency"] == "quarterly"}
    groups: dict[tuple, dict[str, list[dict]]] = {}
    for r in rows:
        if (r.get("derived") is not None or str(r.get("original_tag") or "").startswith(_TAG_PREFIX)
                or r["concept"] not in eligible
                or r.get("period_start") is None or r.get("value") is None):
            continue
        key = (r["concept"], r["entity_scheme"], r["entity_value"], r["basis"], r["source_code"],
               r["unit"], r["period_start"])
        groups.setdefault(key, {}).setdefault(r["frequency"], []).append(r)
    lo, hi = QUARTER_BAND
    out: list[dict] = []
    for by_freq in groups.values():
        for earlier_f, later_f, formula in PAIRS:
            for later in by_freq.get(later_f, ()):
                for earlier in by_freq.get(earlier_f, ()):
                    if _quarter_key(later) in have:
                        continue
                    start = earlier["valid_date"] + timedelta(days=1)
                    if not lo <= (later["valid_date"] - start).days + 1 <= hi:
                        counters["quarters:not_a_quarter"] += 1
                        continue
                    q = dict(later)
                    q.update(frequency="quarterly", period_start=start,
                             value=later["value"] - earlier["value"], location=_LOCATION,
                             original_tag=_TAG_PREFIX + later["original_tag"], derived=formula,
                             conflict=bool(earlier.get("conflict")) or bool(later.get("conflict")),
                             on_statement=bool(earlier.get("on_statement"))
                             and bool(later.get("on_statement")))
                    have.add(_quarter_key(q))
                    out.append(q)
    out.sort(key=lambda r: (r["concept"], r["valid_date"], r["entity_scheme"], r["entity_value"],
                            r["basis"], r["source_code"], r["unit"]))
    return rows + out


def hidden_semiannual(rows: Iterable[dict]) -> set[date]:
    """The ``valid_date`` values of ``semiannual`` rows at which a ``quarterly`` row ends: those
    6-month rows are derivation inputs and leave the default output (spec rule 6).

    Expects ONE issuer's rows (the result carries dates only); rows of another
    ``(entity_scheme, entity_value)`` never hide. Any quarterly row matches: filed or derived,
    any concept, basis or source. Decided per period, so a semiannual reporter, or an older
    semiannual year of an issuer that later filed quarters, keeps its 6-month rows (rule 10).
    A genuine H2 6-month row ending at a fiscal year-end that has a (derived) Q4 is hidden too:
    the issuer files quarters that year (rule 6).
    """
    rows = list(rows)
    quarter_ends = {(r["entity_scheme"], r["entity_value"], r["valid_date"])
                    for r in rows if r["frequency"] == "quarterly"}
    return {r["valid_date"] for r in rows if r["frequency"] == "semiannual"
            and (r["entity_scheme"], r["entity_value"], r["valid_date"]) in quarter_ends}


def metrics_from_derived(summary: PeriodSummary) -> frozenset[str]:
    """Keys of ``summary.derived`` that rest on a derived quarter (spec rule 8): the metric's
    value differs, or the metric disappears, when :func:`compute_derived` runs on the summary's
    values without the entries whose ``derived`` is not None (a value with no ``derived`` key
    counts as filed). By this definition a metric that reads a derived input but whose value
    does not move without it (e.g. a derived component worth 0, or a derived total equal to its
    filed fallback) is not marked. Empty when no value is derived.
    """
    filed = {k: v for k, v in summary.values.items() if v.get("derived") is None}
    if len(filed) == len(summary.values):
        return frozenset()
    full = summary.derived
    without = compute_derived(filed, summary.frequency, summary.currency, summary.is_financial,
                              summary.debt_rule)
    return frozenset(k for k, m in full.items()
                     if k not in without or without[k].get("value") != m.get("value"))
