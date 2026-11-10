"""Store rows -> period summaries with ratios: the store readers' entry point (the MCP calls it).

Input: the rows :func:`~company_corpus.concepts.selection.run_selection` returns for ONE issuer
(my-open-data ``fundamentals`` vintages and ``filing_statements`` as the Recipe reads them).
Output: :class:`~company_corpus.financials.PeriodSummary` objects whose ``values`` are F1's and
whose ``derived`` (point-in-time) and ``ttm`` ratios come from the same functions as F1, so a
store reader never computes a concept or a ratio itself.

The store carries the 9-month YTD band (``ytd9``, fundamentals 3.1.0), so the TTM Q4 rule defaults
to F1's ``q4_rule="ytd9"`` (Q4 = annual - 9-month YTD); ``"sum_of_quarters"`` (Q4 = annual - Q1 - Q2
- Q3, spec MCP-2 Q6) stays a parameter.

Publication date: pass the issuer's ``fundamentals`` rows as ``vintages`` (see
:func:`summaries_from_rows`). ``known_at`` is a UTC instant (my-open-data declares it
``timestamp("us", tz="UTC")``, so store rows arrive tz-aware; a reader may hand it back in its
session zone), read as a calendar date in ``publication_tz`` (default ``"America/New_York"``,
EDGAR's zone and the store's own ``store_known_at_fallback_tz``): a filing without an acceptance
time gets 23:59:59 New York, which is already the next day in UTC. On real store rows, which
carry EDGAR acceptance times (unlike the fixtures, built with no acceptance map), EDGAR gives a
filing accepted after 17:30 ET the next business day's ``filingDate``: for such late-accepted
filings ``publication_date`` can precede F1's (which uses ``filingDate``) by 1-3 days, and the
``>= period end`` filter can differ at that boundary. The same boundary reaches values, not
only the date: the selection's ``filed_before_period_end`` rule reads the store's acceptance
instant (F1: the ``filingDate``), so a filing accepted after 17:30 ET in the business days just
before a period end can be early in the store yet on time in F1 (never the reverse: the
``filingDate`` is never before the acceptance date); near a period end a ``drop`` concept's value,
not only the publication date, can then differ between F1 and the store. When the producer caps ``known_at`` at its
run time (``now``), the New York date can also fall before the filing date.

Currency: per period, the first ISO-4217 (3-letter) unit among the period's values in dictionary
order, else ``"USD"``; a mixed-currency period takes the dictionary-first concept's ISO unit. It
equals F1's issuer-wide ``reporting_currency`` on every concept fixture; the producer already
keeps one currency per issuer filing (its currency anchor tags).

Two known differences from F1 that no fixture exercises: the first filing is picked by
``(known_at, filing_ref)`` where F1 takes point order, so two filings the same day (a 10-K and its
10-K/A) can give another accession with the same date; and publication-date candidates are not
filtered to the reporting currency, where F1 drops points in another currency.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ..financials import (
    _CURRENCY_RE,
    DERIVED,
    DERIVED_TTM,
    PeriodSummary,
    Q4Rule,
    _attach_ttm,
    _TTM_FLOW_KEYS,
)
from .dictionary import Dictionary, load_dictionary
from .selection import DEFAULT_FILING_TZ
from .summaries import _F1_LABEL, _flow_series, _value, group_periods

__all__ = ["DEFAULT_PUBLICATION_TZ", "concept_catalog", "group_periods", "metric_catalog",
           "summaries_from_rows"]

#: EDGAR's zone: a filing's date is its acceptance instant read there. The same zone as the
#: selection's ``DEFAULT_FILING_TZ`` (one zone, two names).
DEFAULT_PUBLICATION_TZ = DEFAULT_FILING_TZ


def summaries_from_rows(rows: Iterable[dict], *, company: str, company_current: str | None = None,
                        sic: str | None = None, sector_known: bool = True,
                        q4_rule: Q4Rule = "ytd9",
                        vintages: Iterable[dict] | None = None,
                        publication_tz: str = DEFAULT_PUBLICATION_TZ,
                        dictionary: Dictionary | None = None) -> list[PeriodSummary]:
    """One summary per allowed duration period of one issuer's selection ``rows``, newest first.

    ``vintages`` are the issuer's ``fundamentals`` rows the selection ran on. The publication
    date is, as in F1, the earliest filing of any of the period's dictionary-tag vintages made
    once the period had ended (its ``known_at`` read as a date in ``publication_tz``), the
    accession that filing's. Without ``vintages`` only the selected rows are at hand: the
    selection keeps the latest vintage per tag and period, so the date is then the first filing
    of a selected row, later than the real first publication whenever a later filing re-reported
    the period (next year's comparatives do). A period with no such filing (only seen before it
    ended) has no summary, as in F1. ``sector_known=False`` (no SIC in the store) leaves every
    ``sector_relevant`` None.
    """
    d = dictionary or load_dictionary()
    zone = ZoneInfo(publication_tz)
    rows = list(rows)

    def utc(at: datetime) -> datetime:  # a naive known_at is UTC, as the store writes it
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)

    def day(at: datetime) -> date:
        return utc(at).astimezone(zone).date()

    namespaces = sorted({s.ns for s in d.tag_specs().values()})
    duration, instant, allowed = group_periods(rows)
    filed_at: dict[tuple[date, str], list[dict]] = {}
    if vintages is not None:
        chain_tags = {full for t in d.tables for e in t.entries.values() for full in e.all_tags}
        # Selection rows always carry their entity keys (synthetic ones may not); vintages may be
        # arbitrary dicts, so they are read with .get and kept only for the rows' issuer.
        issuers = {(r["entity_scheme"], r["entity_value"]) for r in rows if "entity_value" in r}
        for v in vintages:
            if v["original_tag"] in chain_tags and (
                    not issuers or (v.get("entity_scheme"), v.get("entity_value")) in issuers):
                filed_at.setdefault((v["valid_date"], v["frequency"]), []).append(v)
        if duration and not filed_at:
            raise ValueError("vintages hold no row of this issuer's selected tags: pass the "
                             "fundamentals rows the selection ran on")
    out: list[PeriodSummary] = []
    for (end, freq), per_concept in duration.items():
        if freq not in allowed:
            continue
        merged = {**per_concept, **instant.get(end, {})}
        candidates = ([*filed_at.get((end, freq), ()), *filed_at.get((end, "instant"), ())]
                      if vintages is not None else merged.values())
        reported = [r for r in candidates if day(r["known_at"]) >= end]
        if not reported:
            continue
        first = min(reported, key=lambda r: (utc(r["known_at"]), r["filing_ref"]))
        values = {k: _value(merged[k], d, namespaces) for k in d.concepts if k in merged}
        out.append(PeriodSummary(
            period_end=end, frequency=_F1_LABEL.get(freq, freq),
            publication_date=day(first["known_at"]), sec_form="XBRL",
            accession=first["filing_ref"], company=company,
            company_current=company_current or company, values=values,
            currency=next((v["unit"] for v in values.values()
                           if _CURRENCY_RE.match(v["unit"])), "USD"),
            sic=sic, sector_known=sector_known, debt_rule="concepts"))
    out.sort(key=lambda s: (s.period_end or date.min, s.frequency), reverse=True)
    _attach_ttm(_flow_series(rows), out, ttm_keys=_TTM_FLOW_KEYS, q4_rule=q4_rule)
    return out


def concept_catalog(dictionary: Dictionary | None = None) -> list[dict]:
    """The dictionary's concepts: ``key``, ``label``, ``kind``, ``statement``, ``instant``.

    ``kind`` is ``money``, ``money/shares`` or ``shares``: a concept has no fixed unit since the
    dictionary spans taxonomies and reporting currencies (a value's unit is on its summary)."""
    d = dictionary or load_dictionary()
    return [{"key": c.key, "label": c.label, "kind": c.kind, "statement": c.statement,
             "instant": c.instant} for c in d.concepts.values()]


def metric_catalog() -> list[dict]:
    """The computed metrics: point-in-time (``DERIVED``) then trailing-twelve-month."""
    return [{"key": m.key, "label": m.label, "unit": m.unit, "ttm": ttm,
             "annual_only": m.annual_only}
            for ttm, metrics in ((False, DERIVED), (True, DERIVED_TTM)) for m in metrics]
