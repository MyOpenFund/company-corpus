"""Map Estonia Äriregister (RIK) bulk-CSV elements to our curated concepts.

Consumes one report's ``elements`` map produced by
:func:`company_corpus.registers.ee_csv.iter_ee_reports` — ``{et_gaap_name:
float}`` where each key is a *standalone* Estonian-GAAP element name (the
``*Consolidated`` variants are already dropped upstream) — and produces the same
shape as the BE/FI/NO siblings: ``{period_end, basis, currency, values,
suppressed, unbalanced}`` with ``values[key] = {"value", "unit", "label",
"tag"}``. ``basis`` is ``"company"`` (RIK standalone statutory accounts); the
currency is EUR.

GOVERNING PRINCIPLE — NO FALSE DATA. This is open data for the credit universe:
a number we cannot confirm must never be emitted; a *missing* number is strictly
better than a *wrong* one. So every value below is either its confirmed,
directly-tagged figure or it is suppressed with a recorded reason.

Two Estonian traps drive the mapping:

* **The net-income trap.** The Estonian P&L waterfall runs
  ``TotalProfitLoss`` (operating result / *ärikasum*) →
  ``TotalProfitLossBeforeTax`` (pretax / *kasum enne tulumaksustamist*) →
  ``TotalAnnualPeriodProfitLoss`` (the FINAL result for the year /
  *aruandeaasta kasum*, **after** income tax). ``net_income`` is ALWAYS
  ``TotalAnnualPeriodProfitLoss`` — **never** the operating ``TotalProfitLoss``
  nor the pretax ``TotalProfitLossBeforeTax``. Mapping either earlier line would
  be a materially false net-income, so the three lines go to three distinct keys
  (``operating_income`` / ``pretax_income`` / ``net_income``) and are never
  conflated.

* **Leverage is liabilities-based.** The RIK bulk carries no borrowings line, so
  ``short_term_debt`` is mapped to ``CurrentLiabilities`` and ``long_term_debt``
  to ``NonCurrentLiabilities``. The engine's ``total_debt`` is then
  ``CurrentLiabilities + NonCurrentLiabilities`` and ``debt_to_equity`` is a
  liabilities-based (not borrowings-based) ratio.

The confidence gate (§4 of the design doc):

* **Company-template guard.** The RIK bulk mixes for-profit and NGO/non-profit
  templates. An NGO report has no ``Assets``/``Equity`` (it uses
  ``LiabilitiesAndNetAssets`` / ``NetSurplusDeficitForPeriod`` instead); mapping
  it into the company schema would emit mislabelled figures. So when either
  ``Assets`` or ``Equity`` is absent we treat the report as **no-financials**
  (empty values, ``unbalanced=False``, reason recorded) rather than guess.
* **Primary balance.** ``Assets == Equity + CurrentLiabilities +
  NonCurrentLiabilities`` within ``tol = max(2, 0.005·|Assets|)`` (absent
  liability buckets count as 0 — a zero-debt filer). Mismatch → the whole filing
  is untrustworthy: ``unbalanced=True``, emit NO values (a wrong balance sheet is
  worse than none).

Always suppressed (record reasons): ``interest_expense`` and
``interest_coverage`` — the RIK bulk contains no interest / borrowings element at
all, so a coverage ratio can never be produced from this source.
"""
from __future__ import annotations

from ._common import _tol

# ---------------------------------------------------------------------------
# The validated concept pack: curated key -> et-gaap element name. Two keys
# deliberately share an element: ``liabilities_current`` and ``short_term_debt``
# are both ``CurrentLiabilities`` (the former feeds working capital, the latter
# the liabilities-based leverage split); ``long_term_debt`` is
# ``NonCurrentLiabilities``. ``net_income`` is the FINAL after-tax line
# ``TotalAnnualPeriodProfitLoss`` — never ``TotalProfitLoss`` (operating) nor
# ``TotalProfitLossBeforeTax`` (pretax); see the module docstring. ``dep_amort``
# is handled separately (it is a negative cost line, stored as its abs()).
# ---------------------------------------------------------------------------
EE_PACK: dict[str, str] = {
    "assets":              "Assets",
    "assets_current":      "CurrentAssets",
    "non_current_assets":  "NonCurrentAssets",
    "cash":                "CashAndCashEquivalents",
    "equity":              "Equity",
    "liabilities_current": "CurrentLiabilities",
    "short_term_debt":     "CurrentLiabilities",       # liabilities-based leverage
    "long_term_debt":      "NonCurrentLiabilities",    # liabilities-based leverage
    "revenue":             "Revenue",
    "operating_income":    "TotalProfitLoss",              # ärikasum (operating)
    "pretax_income":       "TotalProfitLossBeforeTax",     # kasum enne tulumaksu.
    "net_income":          "TotalAnnualPeriodProfitLoss",  # FINAL — after tax
}

# ``dep_amort``'s source element is a negative cost line (*põhivarade kulum*);
# we store its absolute value so the engine's ``ebitda = operating_income +
# dep_amort`` adds it back correctly.
_DEP_AMORT_ELEMENT = "DepreciationAndImpairmentLossReversal"

# Concepts we never emit for EE, with the reason recorded in ``suppressed``.
_ALWAYS_SUPPRESS: dict[str, str] = {
    "interest_expense": "RIK bulk carries no interest-expense element — "
                        "suppressed (no false data)",
    "interest_coverage": "no interest/borrowings element exists in the RIK bulk "
                         "— coverage ratio not computable — suppressed "
                         "(no false data)",
}


def map_ee_report(
    elements: dict[str, float],
    period_end: str | None,
    registrikood: str | None,
) -> dict:
    """One EE report's ``elements`` map -> curated financials for the year.

    ``elements`` is ``{et_gaap_name: float}`` (standalone only). Returns
    ``{period_end, basis, currency, values, suppressed, unbalanced}``: ``basis``
    is ``"company"``; ``currency`` is ``"EUR"``; ``values[key] = {"value",
    "unit":"EUR", "label":key, "tag":"et-gaap:<Name>"}``; ``suppressed`` is a
    list of ``(key, reason)``; ``unbalanced`` is True when the primary balance
    gate fails (then ``values`` is empty). An NGO/non-profit template (no
    ``Assets``/``Equity``) yields empty values with ``unbalanced=False``
    (no-financials).

    ``registrikood`` is accepted for symmetry with the sibling mappers and to
    aid provenance in log/error messages; it does not affect the mapping.
    """
    elements = elements or {}
    values: dict[str, dict] = {}
    suppressed: list[tuple[str, str]] = []

    def emit(key: str, name: str, value: float) -> None:
        values[key] = {"value": value, "unit": "EUR", "label": key,
                       "tag": f"et-gaap:{name}"}

    def suppress(key: str, reason: str) -> None:
        suppressed.append((key, reason))

    def result(unbalanced: bool) -> dict:
        return {
            "period_end": period_end, "basis": "company", "currency": "EUR",
            "values": values, "suppressed": suppressed, "unbalanced": unbalanced,
        }

    assets = elements.get("Assets")
    equity = elements.get("Equity")

    # --- Company-template guard. The RIK bulk mixes for-profit and NGO templates.
    #     An NGO/non-profit report has no Assets/Equity (it reports
    #     LiabilitiesAndNetAssets / NetSurplusDeficitForPeriod); mapping it into the
    #     company schema would emit mislabelled figures -> no-financials, no values
    #     (a distinct status from `unbalanced`).
    if assets is None or equity is None:
        missing = "Assets" if assets is None else "Equity"
        suppress("__all__",
                 f"no-financials: for-profit balance-sheet anchor {missing} absent "
                 "— likely an NGO/non-profit template (LiabilitiesAndNetAssets / "
                 "NetSurplusDeficitForPeriod); not mapped into the company schema")
        return result(unbalanced=False)

    # --- Primary balance gate: Assets == Equity + CurrentLiabilities +
    #     NonCurrentLiabilities within tol. Absent liability buckets count as 0
    #     (a zero-debt filer). Mismatch -> the whole filing is untrustworthy:
    #     unbalanced, emit NO values (a wrong balance sheet is worse than none).
    cl = elements.get("CurrentLiabilities") or 0.0
    ncl = elements.get("NonCurrentLiabilities") or 0.0
    if abs(assets - (equity + cl + ncl)) > _tol(assets):
        suppress("__all__",
                 f"unbalanced: Assets {assets} != Equity {equity} + "
                 f"CurrentLiabilities {cl} + NonCurrentLiabilities {ncl}")
        return result(unbalanced=True)

    # --- Confirmed concept pack: each key is its directly-tagged value, or a
    #     recorded absence (only emit a key if its element is present).
    for key, name in EE_PACK.items():
        raw = elements.get(name)
        if raw is None:
            suppress(key, f"et-gaap:{name} absent on the filing")
        else:
            emit(key, name, raw)

    # --- Total liabilities = CurrentLiabilities + NonCurrentLiabilities — the same
    #     value the balance gate already uses (absent bucket counts as 0). Emitted
    #     when either bucket is tagged; the six sibling registers all carry a total
    #     `liabilities`, so EE emits one too rather than only the CL/NCL split.
    cl_raw = elements.get("CurrentLiabilities")
    ncl_raw = elements.get("NonCurrentLiabilities")
    if cl_raw is not None or ncl_raw is not None:
        values["liabilities"] = {
            "value": (cl_raw or 0.0) + (ncl_raw or 0.0), "unit": "EUR",
            "label": "liabilities",
            "tag": "et-gaap:CurrentLiabilities + NonCurrentLiabilities (derived)",
        }

    # --- dep_amort: the source element is a negative cost line; store abs() so
    #     the engine's ebitda = operating_income + dep_amort adds it back.
    raw_da = elements.get(_DEP_AMORT_ELEMENT)
    if raw_da is None:
        suppress("dep_amort", f"et-gaap:{_DEP_AMORT_ELEMENT} absent on the filing")
    else:
        emit("dep_amort", _DEP_AMORT_ELEMENT, abs(raw_da))

    # --- Always-suppressed: no interest / borrowings element in the bulk, so
    #     interest_expense and interest_coverage can never be produced.
    for key, reason in _ALWAYS_SUPPRESS.items():
        suppress(key, reason)

    return result(unbalanced=False)
