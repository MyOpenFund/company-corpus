"""DK register concept mapping — two paths.

Path A (listed ESEF/IFRS):  parse_virk_esef_xml + map_dk_esef
    Stdlib ``xml.etree`` parser for Virk bare ``ifrs-full`` XBRL instances.
    Produces the same flat ``{local_name: [datapoint]}`` shape as
    ``xbrl.oim.flatten_oim_json``, then feeds it through the existing
    ``summaries_from_flat(flat, concepts=IFRS_CONCEPTS)`` — 100 % reuse,
    no Arelle, borrowings-based leverage for free.

Path B (private DK-GAAP FSA):  map_fsa_facts
    Map Denmark FSA (Erhvervsstyrelsen) DK-GAAP facts to our curated concepts.

Consumes the flat fact map produced by
:func:`company_corpus.registers.dk_fsa_xbrl.parse_fsa_facts` —
``{"period_end", "currency", "facts": {local_name: float}}`` where each key is
the FSA element local name — and produces the same shape as the BE/FI/NO/UK
siblings: ``{period_end, basis, currency, values, suppressed, unbalanced}`` with
``values[key] = {"value", "unit", "label", "tag"}``. ``basis`` is ``"company"``
(DK-GAAP individual accounts); the currency is **DKK** (never EUR).

GOVERNING PRINCIPLE — NO FALSE DATA. This is open data for the credit universe:
a number we cannot confirm must never be emitted; a *missing* number is strictly
better than a *wrong* one. So every value below is either its confirmed,
directly-tagged figure (or a clean derivation) or it is suppressed with a
recorded reason.

Two DK-GAAP traps drive the gate (ÅRL — the Danish Financial Statements Act):

* **The GrossProfitLoss ≠ revenue trap (§32).** Class-B / micro filers may
  present *Bruttoresultat* (a gross result = revenue − COGS − external costs)
  under ``fsa:GrossProfitLoss`` and **omit** ``fsa:Revenue`` entirely. Mapping
  ``GrossProfitLoss`` to ``revenue`` would badly understate the top line, so we
  **never** do it: ``revenue`` is emitted only from ``fsa:Revenue`` and is
  suppressed when that tag is absent. ``fsa:GrossProfitLoss`` is mapped to its
  own ``gross_profit`` key, correctly labelled.

* **The borrowings-by-instrument trap (leverage).** DK-GAAP class-B does **not**
  tag borrowings by instrument (bank vs bond vs lease), so a debt split by
  instrument can never be fabricated (``financial_debt`` is always suppressed).
  What DK-GAAP *does* provide is a **maturity** split of the non-provision
  liabilities: ``fsa:ShorttermLiabilitiesOtherThanProvisions`` and
  ``fsa:LongtermLiabilitiesOtherThanProvisions`` (the taxonomy names which is
  short vs long — no guessing needed), plus ``fsa:Provisions`` reported
  separately. We emit the maturity buckets as ``short_term_debt`` /
  ``long_term_debt`` **only** when they are directly tagged AND
  ``short + long + provisions`` reconciles to the derived total liabilities
  (i.e. the split fully accounts for the balance sheet); otherwise the split is
  suppressed. Provisions stay a separate reported value and are **never** folded
  into the debt keys.

The confidence gate (§4 of the design doc):

* **Primary balance:** ``Assets == LiabilitiesAndEquity`` within tolerance.
  Mismatch → the whole filing is untrustworthy: ``unbalanced=True``, no values
  (a wrong balance sheet is worse than none). When either anchor is absent the
  gate cannot run; we proceed (directly-tagged values still stand), mirroring
  the BE/FI/UK siblings.
* **Total liabilities** are *derived* as ``LiabilitiesAndEquity − Equity`` — this
  captures provisions automatically (provisions are part of the passiv total but
  are excluded from ``LiabilitiesOtherThanProvisions``), so no separate
  provisions gate is needed.
"""
from __future__ import annotations

import xml.etree.ElementTree as _ET
from datetime import date as _date

from ._common import _tol

# ---------------------------------------------------------------------------
# The curated concept pack: curated key -> (FSA local name, human label). Each
# entry is a directly-tagged figure; emitted when present, suppressed (with a
# recorded reason) when absent. ``liabilities`` is derived (below) and
# ``revenue`` / the maturity split carry extra NO-FALSE-DATA logic.
# ---------------------------------------------------------------------------
DK_PACK: dict[str, tuple[str, str]] = {
    "assets":         ("Assets", "Total assets"),
    "assets_current": ("CurrentAssets", "Current assets"),
    "cash":           ("CashAndCashEquivalents", "Cash and cash equivalents"),
    "equity":         ("Equity", "Equity"),
    "net_income":     ("ProfitLoss", "Profit/loss for the year"),
    "gross_profit":   ("GrossProfitLoss", "Gross profit/loss (Bruttoresultat)"),
}

# FSA local names used by the derivations / gate (not in the plain pack loop).
_ASSETS = "Assets"
_LIAB_AND_EQUITY = "LiabilitiesAndEquity"
_EQUITY = "Equity"
_REVENUE = "Revenue"
_PROVISIONS = "Provisions"
_SHORT = "ShorttermLiabilitiesOtherThanProvisions"
_LONG = "LongtermLiabilitiesOtherThanProvisions"


def map_fsa_facts(parsed: dict) -> dict:
    """One ``parse_fsa_facts`` result -> curated DK-GAAP financials.

    ``parsed`` is ``{"period_end", "currency", "facts": {local_name: float}}``.
    Returns ``{period_end, basis, currency, values, suppressed, unbalanced}``:
    ``basis`` is ``"company"``; ``values[key] = {"value", "unit":"DKK", "label",
    "tag":"fsa:<Name>"}``; ``suppressed`` is a list of ``(key, reason)``;
    ``unbalanced`` is True when the primary balance gate fails (then ``values``
    is empty).
    """
    facts: dict[str, float] = parsed.get("facts") or {}
    period_end = parsed.get("period_end")
    currency = parsed.get("currency") or "DKK"

    values: dict[str, dict] = {}
    suppressed: list[tuple[str, str]] = []

    def emit(key: str, value: float, tag: str, label: str) -> None:
        # ÅRL §16 permits DK annual reports in EUR; use the detected currency
        # (never hardcode "DKK" — a per-value unit mismatch is false data).
        values[key] = {"value": value, "unit": currency, "label": label, "tag": tag}

    def suppress(key: str, reason: str) -> None:
        suppressed.append((key, reason))

    def result(unbalanced: bool) -> dict:
        return {
            "period_end": period_end, "basis": "company", "currency": currency,
            "values": values, "suppressed": suppressed, "unbalanced": unbalanced,
        }

    assets = facts.get(_ASSETS)
    liab_and_equity = facts.get(_LIAB_AND_EQUITY)
    equity = facts.get(_EQUITY)

    # --- Primary balance gate: Assets == LiabilitiesAndEquity. On mismatch the
    #     whole filing is untrustworthy -> unbalanced, emit NO values. When
    #     either anchor is absent the gate cannot run; we proceed (directly-tagged
    #     values still stand), mirroring the BE/FI/UK siblings.
    if assets is not None and liab_and_equity is not None:
        scale = max(abs(assets), abs(liab_and_equity))
        if abs(assets - liab_and_equity) > _tol(scale):
            suppress("__all__",
                     f"unbalanced: Assets {assets} != LiabilitiesAndEquity "
                     f"{liab_and_equity} beyond tol")
            return result(unbalanced=True)

    # --- Plain confirmed pack: directly-tagged value or a recorded absence.
    for key, (local, label) in DK_PACK.items():
        raw = facts.get(local)
        if raw is None:
            suppress(key, f"fsa:{local} absent on the filing")
        else:
            emit(key, raw, f"fsa:{local}", label)

    # --- Total liabilities, DERIVED as LiabilitiesAndEquity − Equity. This
    #     captures provisions automatically (they are part of the passiv total).
    liabilities: float | None = None
    if liab_and_equity is not None and equity is not None:
        liabilities = liab_and_equity - equity
        emit("liabilities", liabilities,
             "fsa:LiabilitiesAndEquity − fsa:Equity (derived)",
             "Total liabilities (derived)")
    else:
        suppress("liabilities",
                 "cannot derive: fsa:LiabilitiesAndEquity or fsa:Equity absent")

    # --- Provisions: a real directly-tagged passiv total, reported separately
    #     (never counted as debt).
    provisions = facts.get(_PROVISIONS)
    if provisions is not None:
        emit("provisions", provisions, "fsa:Provisions", "Provisions")

    # --- Revenue (§32 trap): emit ONLY from fsa:Revenue. When it is absent the
    #     filer is presenting Bruttoresultat instead; GrossProfitLoss is NOT
    #     revenue, so we suppress rather than substitute it.
    revenue = facts.get(_REVENUE)
    if revenue is not None:
        emit("revenue", revenue, "fsa:Revenue", "Revenue")
    else:
        suppress("revenue",
                 "fsa:Revenue absent (ÅRL §32: filer presents Bruttoresultat; "
                 "fsa:GrossProfitLoss is NOT revenue — never substituted)")

    # --- Leverage: maturity split (NO FALSE DATA). Emit short_term_debt /
    #     long_term_debt only when the taxonomy-labelled buckets are present AND
    #     short + long + provisions reconciles to the derived total liabilities
    #     (the split fully accounts for the passiv). Otherwise suppress the split
    #     — never map a lone total as long_term_debt, never guess a maturity.
    short = facts.get(_SHORT)
    long = facts.get(_LONG)
    _emit_maturity_split(emit, suppress, liabilities, short, long, provisions)

    # --- Borrowings-by-instrument is never available for class-B filers; a debt
    #     split by instrument is never fabricated (liabilities-based leverage only).
    suppress("financial_debt",
             "DK-GAAP class-B does not tag borrowings by instrument — no "
             "bank/bond/lease split; never fabricated (no false data)")

    return result(unbalanced=False)


def _emit_maturity_split(emit, suppress, liabilities, short, long, provisions):
    """Emit ``short_term_debt`` / ``long_term_debt`` from the DK-GAAP maturity
    buckets, or suppress the split atomically with a recorded reason.

    The buckets are ``fsa:ShorttermLiabilitiesOtherThanProvisions`` (short) and
    ``fsa:LongtermLiabilitiesOtherThanProvisions`` (long) — the taxonomy labels
    which is which, so no guessing is involved. They are trusted only when they
    reconcile: ``short + long + provisions`` must equal the derived total
    liabilities within tolerance, i.e. the maturity split (plus provisions) fully
    accounts for the passiv. On reconciliation each *present* bucket is emitted
    with its confirmed label; a bucket the filing did not tag is left absent
    (never synthesised — provisions/equity make a 0 long-term genuinely unknown
    as a *reported* line). Provisions stay separate and are never added to debt.
    """
    keys = ("short_term_debt", "long_term_debt")

    def suppress_both(reason: str) -> None:
        for key in keys:
            suppress(key, reason)

    if liabilities is None:
        suppress_both("no derived total liabilities to reconcile the maturity "
                      "split against — split suppressed")
        return
    if short is None and long is None:
        suppress_both("no maturity buckets tagged "
                      "(fsa:Shortterm/LongtermLiabilitiesOtherThanProvisions) — "
                      "no split to emit")
        return

    recon = (short or 0.0) + (long or 0.0) + (provisions or 0.0)
    if abs(recon - liabilities) > _tol(abs(liabilities)):
        suppress_both(
            f"maturity split does not reconcile: short {short or 0.0} + long "
            f"{long or 0.0} + provisions {provisions or 0.0} = {recon} != "
            f"derived liabilities {liabilities} beyond tol — suppressed "
            "(never map a lone total as long_term_debt)")
        return

    # Reconciled: emit each bucket the taxonomy actually tagged.
    if short is not None:
        emit("short_term_debt", short,
             "fsa:ShorttermLiabilitiesOtherThanProvisions",
             "Short-term liabilities other than provisions")
    else:
        suppress("short_term_debt",
                 "no fsa:ShorttermLiabilitiesOtherThanProvisions on the filing")
    if long is not None:
        emit("long_term_debt", long,
             "fsa:LongtermLiabilitiesOtherThanProvisions",
             "Long-term liabilities other than provisions")
    else:
        suppress("long_term_debt",
                 "no fsa:LongtermLiabilitiesOtherThanProvisions on the filing "
                 "(short bucket reconciles alone — long-term total not tagged, "
                 "never synthesised)")


# ===========================================================================
# Path A — Virk ESEF bare-XBRL parser (listed issuers, IFRS)
# ===========================================================================

# XBRL instance namespace URIs
_NS_XBRLI = "http://www.xbrl.org/2003/instance"
_NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

_TAG_CTX = f"{{{_NS_XBRLI}}}context"
_TAG_PERIOD = f"{{{_NS_XBRLI}}}period"
_TAG_START = f"{{{_NS_XBRLI}}}startDate"
_TAG_END = f"{{{_NS_XBRLI}}}endDate"
_TAG_INSTANT = f"{{{_NS_XBRLI}}}instant"
_TAG_ENTITY = f"{{{_NS_XBRLI}}}entity"
_TAG_SCENARIO = f"{{{_NS_XBRLI}}}scenario"
_TAG_SEGMENT = f"{{{_NS_XBRLI}}}segment"
_TAG_UNIT = f"{{{_NS_XBRLI}}}unit"
_TAG_MEASURE = f"{{{_NS_XBRLI}}}measure"
_ATTR_NIL = f"{{{_NS_XSI}}}nil"


def parse_virk_esef_xml(xml_bytes: bytes) -> "dict[str, list[dict]]":
    """Parse a Virk bare ``ifrs-full`` XBRL instance into the engine's flat shape.

    Returns ``{local_name: [datapoint]}`` where each datapoint mirrors the keys
    produced by ``xbrl.oim.flatten_oim_json``:

    * **Instant** (balance-sheet) facts: ``val``, ``end``, ``unit``, ``tag``,
      ``label``, ``filed``, ``form``, ``accn`` — no ``start`` key.
    * **Duration** (P&L / cash-flow) facts: same keys plus ``start``.

    Only **no-dimension** contexts (``xbrli:context`` with no children under
    ``xbrli:scenario``) are indexed. Contexts with a non-empty
    ``xbrli:scenario`` (equity-component splits, maturity buckets, …) are
    dropped, preventing any disaggregation from leaking into the top-line.

    The ``ifrs-full`` namespace URI is matched by URI substring (handles both
    the ``2023-03-27`` and ``2024-03-27`` taxonomy vintages without relying on
    the ``ifrs-full`` prefix, which filing tools can remap). Extension-taxonomy
    facts (non-``ifrs-full`` namespaces) are silently excluded.
    """
    root = _ET.fromstring(xml_bytes)

    # ---- 1. Locate the ifrs-full namespace URI by substring ----
    ifrs_ns: "str | None" = None
    for elem in root.iter():
        tag = elem.tag
        if tag.startswith("{"):
            uri = tag[1:tag.index("}")]
            if "ifrs.org" in uri and "ifrs-full" in uri:
                ifrs_ns = uri
                break
    if ifrs_ns is None:
        return {}

    ifrs_prefix = f"{{{ifrs_ns}}}"

    # ---- 2. Index no-dimension contexts ----
    nodim: "dict[str, dict]" = {}          # ctx_id -> {start, end, is_instant}
    for ctx in root.iter(_TAG_CTX):
        ctx_id = ctx.get("id", "")
        scenario = ctx.find(_TAG_SCENARIO)
        # XBRL 2.1: xbrli:segment is inside xbrli:entity (a child of xbrli:context),
        # not a direct child of xbrli:context itself.
        entity = ctx.find(_TAG_ENTITY)
        segment = entity.find(_TAG_SEGMENT) if entity is not None else None
        # Treat the context as dimensioned (exclude from the no-dim index) when
        # EITHER xbrli:scenario OR xbrli:segment has element children, so a fact
        # dimensioned via segment cannot leak as a top-line value.
        if (scenario is not None and len(list(scenario)) > 0) or \
                (segment is not None and len(list(segment)) > 0):
            continue                       # dimensioned — exclude
        period = ctx.find(_TAG_PERIOD)
        if period is None:
            continue
        instant_el = period.find(_TAG_INSTANT)
        if instant_el is not None:
            try:
                d = _date.fromisoformat((instant_el.text or "").strip()[:10])
            except ValueError:
                continue
            nodim[ctx_id] = {"start": None, "end": d.isoformat(), "is_instant": True}
        else:
            start_el = period.find(_TAG_START)
            end_el = period.find(_TAG_END)
            if start_el is None or end_el is None:
                continue
            try:
                s = _date.fromisoformat((start_el.text or "").strip()[:10])
                e = _date.fromisoformat((end_el.text or "").strip()[:10])
            except ValueError:
                continue
            nodim[ctx_id] = {"start": s.isoformat(), "end": e.isoformat(), "is_instant": False}

    # ---- 3. Build unit -> currency map ----
    unit_map: "dict[str, str]" = {}
    for unit_el in root.iter(_TAG_UNIT):
        uid = unit_el.get("id", "")
        measure_el = unit_el.find(_TAG_MEASURE)
        if measure_el is not None and measure_el.text:
            m = measure_el.text.strip()
            if m.startswith("iso4217:"):
                unit_map[uid] = m.split(":", 1)[1]

    # ---- 4. Extract ifrs-full numeric facts from no-dim contexts ----
    out: "dict[str, list[dict]]" = {}
    for elem in root:
        tag = elem.tag
        if not tag.startswith(ifrs_prefix):
            continue                       # not ifrs-full — exclude extension facts
        if elem.get(_ATTR_NIL, "").lower() == "true":
            continue
        ctx_ref = elem.get("contextRef")
        if ctx_ref not in nodim:
            continue
        unit_ref = elem.get("unitRef")
        if unit_ref is None:
            continue                       # non-numeric / text / boolean fact
        currency = unit_map.get(unit_ref)
        if not currency:
            continue                       # non-monetary unit (xbrli:pure, shares, …)
        text = (elem.text or "").strip()
        if not text:
            continue
        try:
            val: "int | float" = int(text) if "." not in text else float(text)
        except ValueError:
            continue

        local = tag[len(ifrs_prefix):]     # strip "{...}" prefix -> local name
        ctx_info = nodim[ctx_ref]
        point: dict = {
            "val": val,
            "end": ctx_info["end"],
            "unit": currency,
            "tag": local,
            "label": local,
            "filed": "",
            "form": "",
            "accn": "",
        }
        if not ctx_info["is_instant"] and ctx_info["start"]:
            point["start"] = ctx_info["start"]

        out.setdefault(local, []).append(point)

    return out


def map_dk_esef(xml_bytes: bytes) -> list:
    """Virk ESEF bare-XBRL → list of :class:`~company_corpus.financials.PeriodSummary`.

    Pipeline: ``parse_virk_esef_xml`` → ``summaries_from_flat(IFRS_CONCEPTS)``
    (100 % reuse of the EU IFRS engine — no Arelle, borrowings-based leverage
    via ``NoncurrentBorrowings`` + ``CurrentBorrowings``). The balance gate
    ``Assets == Equity + Liabilities`` is enforced: summaries that fail the
    gate are **dropped** and never emitted (parity with all other register
    engines — an unbalanced ESEF summary must not be propagated downstream).

    Returns a list of :class:`PeriodSummary` objects (same type that
    ``eu.financials.build_eu_financials`` produces), ready for
    :func:`~company_corpus.financials.rows_from_base`.
    """
    from ..financials import summaries_from_flat
    from ..xbrl import IFRS_CONCEPTS
    import warnings

    flat = parse_virk_esef_xml(xml_bytes)
    # E-I4 (sector honesty): a DK-ESEF filer is identified by LEI only — no SIC/NACE,
    # so its sector is UNKNOWN. Pass sector_known=False (mirroring the EU pillar in
    # eu/financials.py) so is_financial resolves to None and the bank/insurer-sensitive
    # derived metrics are stamped sector_relevant=None instead of a false True.
    summaries = summaries_from_flat(
        flat, concepts=IFRS_CONCEPTS,
        company="", company_current="", sic=None, sector_known=False,
    )

    # Balance gate: Assets == Equity + Liabilities within tolerance.
    # Parity with all other registers: DROP the summary when the gate fails rather
    # than only warning — an unbalanced ESEF filing must NOT be emitted as "ok".
    balanced: list = []
    for s in summaries:
        v = s.values
        assets = (v.get("assets") or {}).get("value")
        equity_v = (v.get("equity") or v.get("equity_total") or {}).get("value")
        liabilities = (v.get("liabilities") or {}).get("value")
        if assets is not None and equity_v is not None and liabilities is not None:
            tol = max(2.0, 0.005 * abs(assets))
            diff = abs(assets - (equity_v + liabilities))
            if diff > tol:
                warnings.warn(
                    f"ESEF balance gate ({s.period_end}): Assets {assets:,.0f} != "
                    f"Equity {equity_v:,.0f} + Liabilities {liabilities:,.0f} "
                    f"(diff={diff:.0f}, tol={tol:.0f}) — summary dropped (unbalanced)",
                    stacklevel=2,
                )
                continue  # DROP: do not emit this period
        balanced.append(s)

    return balanced
