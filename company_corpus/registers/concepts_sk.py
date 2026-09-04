"""Map a Slovak registeruz.sk účtovný výkaz to our curated financial concepts.

Consumes :func:`company_corpus.registers.sk_registeruz.parse_vykaz` — a
``{idSablony, pristupnostDat, ico, cells:{(table, cisloRiadku): [cols]}}`` dict —
and produces the same shape as the BE/FI siblings:
``{period_end, basis, currency, values, suppressed, unbalanced}`` with
``values[key] = {"value", "unit":"EUR", "label", "tag":"sk:r<cislo>"}``.
``basis`` is ``"company"`` (statutory single-entity accounts); the currency is
always EUR.

GOVERNING PRINCIPLE — NO FALSE DATA. This is open data for the credit universe:
a number we cannot confirm must never be emitted; a *missing* number is strictly
better than a *wrong* one. Two Slovak traps drive the pack:

* **The net-turnover trap (revenue).** ``revenue`` is ``operating_revenue_total``
  — POD row 2 (*Výnosy z hospodárskej činnosti spolu*), MUJ row 1 — **never**
  ``net_turnover`` (POD row 1, *Čistý obrat*), which folds in financial income and
  overstates sales (2 005 372 operating vs 1 950 307 net turnover on the validated
  filing). We map ``revenue`` to the operating-revenue line only and record the
  ``net_turnover`` line as an explicit suppression on POD.

* **The column trap.** POD filings carry the balance-sheet assets table with FOUR
  data columns (brutto / korekcia / netto-current / netto-prior); the meaningful
  current figure is **netto = column index 2**. Every other table (POD passives &
  income statement, and all MUJ tables) has two columns → the current period is
  **column index 0**. Reading the wrong column silently substitutes a gross or a
  prior-year number, so the column rule is part of the correctness heart.

The confidence gate (§4-5 of the design doc), ``tol = max(2, 0.005·|assets|)``:

* **POD balance:** ``|assets − (equity + liabilities + accruals_liab)| ≤ tol``.
  The Slovak passive side splits into equity, liabilities *and* a separate
  accruals/deferrals total (*Časové rozlíšenie*, r141) — so the identity only
  closes with the accruals term. Mismatch → the whole filing is untrustworthy:
  ``unbalanced=True``, no values (a wrong balance sheet is worse than none).
* **MUJ balance:** ``|assets − (equity + liabilities)| ≤ tol`` — the micro
  template has no separate accruals total on the passive side.

Leverage is BORROWINGS-based. ``long_term_debt`` (POD r121 / MUJ r37) and
``short_term_debt`` (POD r139 / MUJ r44) are *bank-loan* lines
(*Dlhodobé / Bežné bankové úvery*). We emit each only when its line is present
AND nonzero — those are real bank borrowings, so the producer stamps
``leverage_basis="borrowings"``. When both are absent/zero the debt block is
suppressed (no leverage rather than a fabricated zero). ``interest_expense`` is
emitted positive as-is (the SK data carries it positive; no ``abs()``).

Suppressed with a recorded reason: ``net_turnover`` (POD — never mapped to
revenue); IFRS (idSablony 695) / other templates; non-public filings
(``pristupnostDat != "Verejné"``); and any curated line absent or non-numeric on
the filing.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

from ._common import _tol
from .sk_registeruz import parse_vykaz

# ---------------------------------------------------------------------------
# The validated concept packs: curated key -> (table_idx, cisloRiadku). The
# column is chosen by ``_col`` (POD assets table = col 2, everything else col 0).
# Every entry below is reconciled to the cent on the committed real fixtures.
# ---------------------------------------------------------------------------
SK_POD_MAP: dict[str, tuple[int, int]] = {
    "assets":             (0, 1),
    "non_current_assets": (0, 2),
    "assets_current":     (0, 33),
    "cash":               (0, 71),
    "equity":             (1, 80),
    "liabilities":        (1, 101),
    "long_term_debt":     (1, 121),   # Dlhodobé bankové úvery — borrowings
    "short_term_debt":    (1, 139),   # Bežné bankové úvery — borrowings
    "revenue":            (2, 2),     # operating_revenue_total — NEVER r1 net_turnover
    "operating_income":   (2, 27),
    "interest_expense":   (2, 49),    # positive as-is
    "pretax_income":      (2, 56),
    "net_income":         (2, 61),
}
# POD-only accruals/deferrals total (Časové rozlíšenie, r141) — used ONLY in the
# balance gate, never emitted as a curated value.
_SK_POD_ACCRUALS: tuple[int, int] = (1, 141)

SK_MUJ_MAP: dict[str, tuple[int, int]] = {
    "assets":             (0, 1),
    "non_current_assets": (0, 2),
    "assets_current":     (0, 14),
    "cash":               (0, 22),
    "equity":             (1, 25),
    "liabilities":        (1, 34),
    "long_term_debt":     (1, 37),    # Dlhodobé bankové úvery — borrowings
    "short_term_debt":    (1, 44),    # Bežné bankové úvery — borrowings
    "revenue":            (2, 1),     # MUJ operating-revenue total
    "operating_income":   (2, 18),
    "interest_expense":   (2, 31),    # positive as-is
    "pretax_income":      (2, 35),
    "net_income":         (2, 38),
}

# Borrowings lines — emitted by the dedicated leverage block (present + nonzero),
# not the plain pack loop.
_LEVERAGE_KEYS = ("long_term_debt", "short_term_debt")

# The two mapped templates: 699 = Úč POD (standard), 687 = Úč MUJ (micro).
_TEMPLATES = {699: SK_POD_MAP, 687: SK_MUJ_MAP}


def cell(cells: dict, key: tuple[int, int], col: int):
    """``cells[key][col]`` as a float, or ``None``.

    Returns ``None`` when the row is absent (line not on the filing), the column
    index is out of range, or the cell itself is empty (parsed to ``None``)."""
    row = cells.get(key)
    if row is None or col >= len(row):
        return None
    return row[col]


def _col(id_sablony: int, table_idx: int) -> int:
    """Column index for a table: POD (699) assets table (0) → netto col 2; else 0."""
    return 2 if (id_sablony == 699 and table_idx == 0) else 0


def _period_end(vykaz: dict) -> str | None:
    """Best-effort period end from ``titulnaStrana.obdobieDo`` (else ``None``).

    Slovak statutory periods end on the last day of the stated month, so a
    ``"YYYY-MM"`` closes at that month's end (``"2023-12"`` → ``"2023-12-31"``).
    A full ISO date is passed through if it parses; anything else → ``None`` (the
    producer then supplies the authoritative period_end from the zavierka)."""
    ts = (vykaz.get("obsah") or {}).get("titulnaStrana") or {}
    raw = ts.get("obdobieDo") or ts.get("obdobie")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})", raw)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        try:
            last = calendar.monthrange(year, month)[1]
        except calendar.IllegalMonthError:
            return None
        return f"{year:04d}-{month:02d}-{last:02d}"
    try:
        date.fromisoformat(raw)
    except ValueError:
        return None
    return raw


def map_sk_vykaz(vykaz: dict, sablona: dict) -> dict:
    """One SK účtovný výkaz (+ its sablona) → curated financials for the period.

    Runs :func:`parse_vykaz` internally, dispatches by ``idSablony``
    (699 → POD, 687 → MUJ, else → no-financials), applies the per-template
    balance gate and the borrowings-based leverage rule.

    Returns ``{period_end, basis:"company", currency:"EUR", values, suppressed,
    unbalanced}``. ``values[key] = {"value", "unit":"EUR", "label":key,
    "tag":"sk:r<cislo>"}``; ``suppressed`` is a list of ``(key, reason)``;
    ``unbalanced`` is True only when the balance gate fails (then ``values`` is
    empty). Non-public / non-mapped filings return empty ``values`` with a
    recorded ``("__all__", reason)`` and ``unbalanced=False``."""
    # --- Template-match guard (NO FALSE DATA — must be the first check).
    #     On the manual --sk-file path an operator can pass a mismatched pair
    #     (e.g. a 687/MUJ vykaz with the 699/POD sablona).  The positional
    #     extractor then indexes with wrong row-order/ncols; gate anchors
    #     resolve to None (gate is skipped) and up to 6 misaligned values are
    #     emitted as plausible-but-wrong data.  Return no-financials
    #     immediately instead.
    _vykaz_sablona_id = vykaz.get("idSablony")
    _sablona_id = sablona.get("id")
    if _sablona_id != _vykaz_sablona_id:
        return {
            "period_end": _period_end(vykaz),
            "basis": "company",
            "currency": "EUR",
            "values": {},
            "suppressed": [("__all__",
                            f"sablona/vykaz idSablony mismatch: "
                            f"sablona.id={_sablona_id!r} != "
                            f"vykaz.idSablony={_vykaz_sablona_id!r}")],
            "unbalanced": False,
        }

    parsed = parse_vykaz(vykaz, sablona)
    id_sablony = parsed["idSablony"]
    pristupnost = parsed["pristupnostDat"]
    cells = parsed["cells"]

    period_end = _period_end(vykaz)
    values: dict[str, dict] = {}
    suppressed: list[tuple[str, str]] = []

    def result(unbalanced: bool) -> dict:
        return {
            "period_end": period_end, "basis": "company", "currency": "EUR",
            "values": values, "suppressed": suppressed, "unbalanced": unbalanced,
        }

    # --- Pre-filter: only public, mapped-template filings with positional tables
    #     carry curated financials. Anything else is no-financials (empty values,
    #     recorded reason) — NOT unbalanced (the data is inaccessible, not wrong).
    if pristupnost != "Verejné":
        suppressed.append(("__all__",
                           f"pristupnostDat={pristupnost!r} != 'Verejné' — "
                           "non-public filing, no financials"))
        return result(unbalanced=False)
    if id_sablony not in _TEMPLATES:
        suppressed.append(("__all__",
                           f"idSablony={id_sablony} not a mapped template "
                           "(699 Úč POD / 687 Úč MUJ) — IFRS(695)/other, suppressed"))
        return result(unbalanced=False)
    if not cells:
        suppressed.append(("__all__",
                           "no positional tables on the vykaz "
                           "(IFRS-PDF / non-public) — no financials"))
        return result(unbalanced=False)

    is_pod = id_sablony == 699
    pack = _TEMPLATES[id_sablony]

    def get(loc: tuple[int, int]):
        return cell(cells, loc, _col(id_sablony, loc[0]))

    def emit(key: str, value: float, cislo: int) -> None:
        values[key] = {"value": value, "unit": "EUR", "label": key,
                       "tag": f"sk:r{cislo}"}

    # --- Balance gate. POD closes with the separate accruals total (r141); MUJ
    #     has no accruals term. On mismatch the whole filing is untrustworthy →
    #     unbalanced, emit NO values. When an anchor is absent the gate cannot run
    #     and directly-tagged values still stand (mirrors the BE/FI siblings).
    assets = get(pack["assets"])
    equity = get(pack["equity"])
    liabilities = get(pack["liabilities"])
    accruals = get(_SK_POD_ACCRUALS) if is_pod else None
    if assets is not None and equity is not None and liabilities is not None:
        rhs = equity + liabilities + (accruals or 0.0)
        if abs(assets - rhs) > _tol(assets):
            detail = (f"equity {equity} + liabilities {liabilities}"
                      + (f" + accruals {accruals or 0.0}" if is_pod else "")
                      + f" = {rhs}")
            suppressed.append(("__all__",
                               f"unbalanced: assets {assets} != {detail} beyond tol"))
            return result(unbalanced=True)

    # --- Plain confirmed pack: directly-tagged current-column value, or a
    #     recorded absence. Leverage lines are handled by the borrowings block.
    for key, loc in pack.items():
        if key in _LEVERAGE_KEYS:
            continue
        val = get(loc)
        if val is None:
            suppressed.append((key, f"sk:r{loc[1]} (table {loc[0]}) absent/empty on the filing"))
        else:
            emit(key, val, loc[1])

    # --- Leverage (BORROWINGS-based). Emit a bank-loan line only when present AND
    #     nonzero — a real borrowing. Both absent/zero → suppress the debt block
    #     (no leverage rather than a fabricated zero). The producer stamps
    #     leverage_basis="borrowings" via _emit_entity_rows — not here.
    for key in _LEVERAGE_KEYS:
        loc = pack[key]
        val = get(loc)
        if val is not None and val != 0:
            emit(key, val, loc[1])
        else:
            suppressed.append((key,
                               f"no bank-loan line (sk:r{loc[1]}) present/nonzero — "
                               "borrowings-based debt not emitted"))

    # --- No-false-data: record the POD net-turnover trap explicitly. r1
    #     (Čistý obrat) includes financial income → it is never mapped to revenue
    #     (revenue = operating_revenue_total, r2).
    if is_pod:
        suppressed.append(("net_turnover",
                           "POD r1 (Čistý obrat) folds in financial income → never "
                           "mapped to revenue; revenue = operating_revenue_total (r2)"))

    return result(unbalanced=False)
