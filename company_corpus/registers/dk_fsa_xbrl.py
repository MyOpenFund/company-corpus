"""Denmark FSA (Erhvervsstyrelsen) DK-GAAP XBRL parser — stdlib only, no Arelle.

Parses a DK-GAAP FSA bare XBRL instance document (as filed with Virk/ERST)
into a flat dict of monetary facts keyed by local name.

Namespace contract
------------------
The DK-GAAP taxonomy namespaces are matched **by URI**, never by prefix, using
Clark notation (``elem.tag == "{uri}LocalName"``).  Prefixes in real filings
may differ from the canonical fsa/gsd/cmn mapping used in the taxonomy docs.

    FSA facts    : http://xbrl.dcca.dk/fsa
    GSD metadata : http://xbrl.dcca.dk/gsd
    CMN commons  : http://xbrl.dcca.dk/cmn

Context selection
-----------------
Private (class-B) DK filings carry no ConsolidatedSoloDimension — their
contexts have no scenario at all.  The selection priority is:

  1. **No-dimension** contexts (no ``xbrli:scenario`` child, or an empty one)
  2. Fall back to ``cmn:ConsolidatedSoloDimension = SoloMember``
  3. Fall back to ``ConsolidatedMember``

Mixed-basis facts (consolidated + solo) are never emitted together.

Current-period selection (NO period-mixing)
-------------------------------------------
A DK-GAAP filing carries the current AND the prior-year figures side by side
(current/prior instant dates for the balance sheet, current/prior duration
periods for the P&L). We anchor on a single **current reporting period** and
emit only that period's facts:

  * ``period_end`` = the **max ``xbrli:instant``** across the selected contexts
    (the current balance-sheet date).
  * a balance-sheet (instant) fact counts only when its context ``instant`` ==
    ``period_end``; a P&L (duration) fact counts only when its context
    ``endDate`` == ``period_end`` (the current fiscal year).
  * a fact tagged **only** in a prior-period context is **excluded** — never
    leaked into the current view, never defaulted. This keeps the emitted row
    internally consistent (e.g. ``Assets == LiabilitiesAndEquity`` holds for the
    current period alone) even when the company changed year over year.

Public API
----------
parse_fsa_facts(source) -> {"period_end": str|None, "currency": str,
                             "facts": {local_name: float}}
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Union

# ── XBRL core namespace URIs ──────────────────────────────────────────────────
_NS_XBRLI = "http://www.xbrl.org/2003/instance"
_NS_XBRLDI = "http://xbrl.org/2006/xbrldi"
_NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

# ── DK-GAAP taxonomy namespace URIs ──────────────────────────────────────────
_NS_FSA = "http://xbrl.dcca.dk/fsa"

# ── Clark-notation tags ───────────────────────────────────────────────────────
_TAG_CONTEXT = f"{{{_NS_XBRLI}}}context"
_TAG_PERIOD = f"{{{_NS_XBRLI}}}period"
_TAG_END_DATE = f"{{{_NS_XBRLI}}}endDate"
_TAG_INSTANT = f"{{{_NS_XBRLI}}}instant"
_TAG_ENTITY = f"{{{_NS_XBRLI}}}entity"
_TAG_SCENARIO = f"{{{_NS_XBRLI}}}scenario"
_TAG_SEGMENT = f"{{{_NS_XBRLI}}}segment"
_TAG_EXPLICIT = f"{{{_NS_XBRLDI}}}explicitMember"
_TAG_UNIT = f"{{{_NS_XBRLI}}}unit"
_TAG_MEASURE = f"{{{_NS_XBRLI}}}measure"

_ATTR_NIL = f"{{{_NS_XSI}}}nil"

# Local name of the DK solo/consolidated dimension (any namespace prefix)
_DIM_SOLO_LOCAL = "ConsolidatedSoloDimension"
_MEM_SOLO = "SoloMember"
_MEM_CONSOLIDATED = "ConsolidatedMember"

# XBRL boilerplate element names — never data facts
_SKIP_LOCAL = frozenset({"context", "unit", "schemaRef"})

# FSA namespace prefix for Clark matching
_FSA_PREFIX = f"{{{_NS_FSA}}}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    """Return the local name from a Clark-notation tag ``{uri}local``."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag.split(":", 1)[-1]


def _parse_date(text: str) -> "date | None":
    """Parse ``YYYY-MM-DD`` (possibly with trailing timestamp) to a date."""
    try:
        return date.fromisoformat(text.strip()[:10])
    except (ValueError, AttributeError):
        return None


def _context_period(ctx: ET.Element) -> "tuple[str | None, date | None]":
    """Classify a context's period as ``(kind, end_date)``.

    ``kind`` is ``"instant"`` when the context carries an ``xbrli:instant`` (a
    balance-sheet snapshot) or ``"duration"`` when it carries an
    ``xbrli:endDate`` (a P&L period, keyed on its end date). ``end_date`` is the
    parsed date used for current-period selection. Returns ``(None, None)`` when
    neither is present or parseable.
    """
    period = ctx.find(_TAG_PERIOD)
    if period is None:
        return (None, None)
    inst = period.find(_TAG_INSTANT)
    if inst is not None and inst.text and inst.text.strip():
        d = _parse_date(inst.text)
        if d is not None:
            return ("instant", d)
    end = period.find(_TAG_END_DATE)
    if end is not None and end.text and end.text.strip():
        d = _parse_date(end.text)
        if d is not None:
            return ("duration", d)
    return (None, None)


def _classify_context(ctx: ET.Element) -> "str | None":
    """Classify a context as 'nodim', 'solo', 'consolidated', or None.

    'None' means the context has dimensional members that are not the
    ConsolidatedSoloDimension — these contexts are ignored.

    XBRL 2.1 §4.7.3 permits dimensional members in EITHER ``xbrli:scenario``
    OR ``xbrli:segment``.  Both are checked so a fact dimensioned via segment
    is never silently promoted to no-dim (false top-line data).
    """
    scenario = ctx.find(_TAG_SCENARIO)
    # XBRL 2.1: xbrli:segment is a child of xbrli:entity (which is itself a
    # child of xbrli:context).  Search via the entity element.
    entity = ctx.find(_TAG_ENTITY)
    segment = entity.find(_TAG_SEGMENT) if entity is not None else None
    scenario_children = list(scenario) if scenario is not None else []
    segment_children = list(segment) if segment is not None else []

    if not scenario_children and not segment_children:
        return "nodim"

    # Scan BOTH containers for an explicit ConsolidatedSoloDimension member.
    # A consolidated/solo member in segment is correctly classified, not silently
    # treated as no-dim.
    for container in (scenario, segment):
        if container is None:
            continue
        for member in container.iter(_TAG_EXPLICIT):
            dim_qname = member.get("dimension", "")
            dim_local = dim_qname.split(":")[-1]
            if dim_local == _DIM_SOLO_LOCAL:
                mem_text = (member.text or "").strip()
                mem_local = mem_text.split(":")[-1]
                if mem_local == _MEM_SOLO:
                    return "solo"
                if mem_local == _MEM_CONSOLIDATED:
                    return "consolidated"

    # Some other dimension (e.g. typed member for board members, or a segment-
    # scoped extension dimension) — exclude from context selection
    return None


def _build_selected_contexts(
    root: ET.Element,
) -> "dict[str, tuple[str | None, date | None]]":
    """Return ``{ctx_id: (kind, end_date)}`` for the selected basis.

    ``kind``/``end_date`` come from :func:`_context_period`. Priority:
    nodim > solo > consolidated. Never mixes bases.
    """
    nodim: dict[str, tuple] = {}
    solo: dict[str, tuple] = {}
    consolidated: dict[str, tuple] = {}

    for ctx in root.iter(_TAG_CONTEXT):
        ctx_id = ctx.get("id", "")
        classification = _classify_context(ctx)
        period = _context_period(ctx)
        if classification == "nodim":
            nodim[ctx_id] = period
        elif classification == "solo":
            solo[ctx_id] = period
        elif classification == "consolidated":
            consolidated[ctx_id] = period

    if nodim:
        return nodim
    if solo:
        return solo
    return consolidated


def _detect_currency(root: ET.Element) -> str:
    """Return the ISO-4217 currency code from the first ``xbrli:unit``.

    Defaults to ``"DKK"`` when no ``iso4217:`` measure is found.
    """
    for unit in root.iter(_TAG_UNIT):
        measure = unit.find(_TAG_MEASURE)
        if measure is not None and measure.text:
            text = measure.text.strip()
            if text.startswith("iso4217:"):
                return text.split(":", 1)[1]
    return "DKK"


# ── Public API ────────────────────────────────────────────────────────────────

def parse_fsa_facts(
    source: Union[str, bytes, Path],
) -> dict:
    """Parse a DK-GAAP FSA XBRL instance into flat monetary facts.

    Parameters
    ----------
    source:
        A file path (``str`` or ``pathlib.Path``) or raw bytes of the XBRL
        instance document.

    Returns
    -------
    dict with keys:
        ``"period_end"``  — ``"YYYY-MM-DD"`` of the current balance-sheet date
                            (max ``xbrli:instant`` across selected contexts), or
                            ``None`` when no dated context is found.
        ``"currency"``    — ISO-4217 code (``"DKK"`` unless the unit says otherwise).
        ``"facts"``       — ``{local_name: float}`` for the numeric ``fsa``-namespace
                            facts of the **current reporting period only**: an
                            instant fact whose context ``instant`` == ``period_end``
                            or a duration fact whose context ``endDate`` ==
                            ``period_end``. A fact tagged only in a prior-period
                            context is excluded (never leaked, never defaulted),
                            so the row is internally consistent for one period.
    """
    if isinstance(source, (str, Path)):
        tree = ET.parse(str(source))
        root = tree.getroot()
    else:
        root = ET.fromstring(source)

    # Build {ctx_id: (kind, end_date)} for the selected basis.
    selected = _build_selected_contexts(root)

    # period_end = the current balance-sheet date = max xbrli:instant across the
    # selected contexts. This anchors the *current* reporting period. When a
    # filing carries no balance-sheet (instant) context at all we fall back to
    # the latest duration end date so a P&L-only document still resolves.
    instant_dates = [d for kind, d in selected.values()
                     if kind == "instant" and d is not None]
    if instant_dates:
        period_end_date: "date | None" = max(instant_dates)
    else:
        dur_dates = [d for _, d in selected.values() if d is not None]
        period_end_date = max(dur_dates) if dur_dates else None
    period_end: "str | None" = (
        period_end_date.isoformat() if period_end_date is not None else None
    )

    # Contexts of the current reporting period: the balance-sheet snapshot at
    # period_end (instant == period_end) AND the fiscal-year P&L ending at
    # period_end (endDate == period_end). Prior-period contexts carry an earlier
    # date and are excluded here, so a prior-only fact can never leak in.
    current_ctx = {
        ctx_id for ctx_id, (_, d) in selected.items()
        if d is not None and d == period_end_date
    }

    # Currency
    currency = _detect_currency(root)

    # Collect fsa-namespace numeric facts from the current-period contexts only.
    facts: dict[str, float] = {}

    for elem in root:
        # Match by namespace URI via Clark prefix — never by prefix name
        if not elem.tag.startswith(_FSA_PREFIX):
            continue

        local = elem.tag[len(_FSA_PREFIX):]  # equivalent to _local() but faster
        if local in _SKIP_LOCAL:
            continue

        ctx_ref = elem.get("contextRef")
        if ctx_ref not in current_ctx:
            continue

        # Skip xsi:nil facts
        if elem.get(_ATTR_NIL, "").lower() == "true":
            continue

        # Must be numeric
        text = (elem.text or "").strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            continue  # text/boolean fact — skip

        # Each balance-sheet / P&L line is tagged once for the current period; on
        # the rare duplicate (same local name repeated in a current-period
        # context) the first occurrence in document order wins.
        if local not in facts:
            facts[local] = value

    return {"period_end": period_end, "currency": currency, "facts": facts}
