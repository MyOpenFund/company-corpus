"""Map Finnish PRH dimensional-XBRL facts to our curated concepts.

Consumes the current-period field map produced by
:func:`company_corpus.registers.fi_prh_xbrl.parse_fi_facts` —
``{"period_end", "currency", "fields": {mcy_int: float}}`` where each key is the
Finnish ``fi_MC:xNNN`` metric code — and produces the same shape as the BE/UK/NO
siblings: ``{period_end, basis, currency, values, suppressed, unbalanced}`` with
``values[key] = {"value", "unit", "label", "tag"}``. ``basis`` is ``"company"``
(FAS individual accounts); the currency is EUR.

GOVERNING PRINCIPLE — NO FALSE DATA. This is open data for the credit universe:
a number we cannot confirm must never be emitted; a *missing* number is strictly
better than a *wrong* one. So every value below is either its confirmed,
directly-tagged figure or it is suppressed with a recorded reason.

Two Finnish traps drive the gate:

* **The net-income trap.** Under Finnish GAAP (FAS) the P&L routes tax through
  *appropriations* (``x541``), so the final bottom line is ``x740``
  (``result after appropriations``), **never** ``x738`` (``result before
  appropriations``): ``x740 = x738 + x541``. We map ``net_income`` to ``x740``
  and verify a two-leg waterfall — if either leg fails we suppress ``net_income``
  rather than fall back to ``x738``.

* **The maturity-label trap (leverage).** ``x583`` and ``x816`` split total
  liabilities (``x513``) by maturity and reconcile to it to the cent, but the
  PRH instance carries **no label linkbase** — the taxonomy that names which
  bucket is long-term vs short-term is external and not shipped with the filing.
  So *which of ``x583``/``x816`` is long vs short cannot be confirmed from the
  data*. Emitting a guessed ``long_term_debt``/``short_term_debt`` split would be
  a false maturity claim, and mapping the whole of ``x513`` into a single bucket
  asserts an equally-false "all one maturity" label. Per NO-FALSE-DATA we
  therefore **suppress the maturity split** (``long_term_debt``/``short_term_debt``)
  entirely — never guess which is long vs short. FI emits ``liabilities`` (``x513``)
  as a raw reported value but produces **no** ``total_debt``/``debt_to_equity``
  (the engine's leverage ratios require the suppressed split). A future authoritative
  codelist confirming the assignment would re-enable the split under the existing
  ``x583 + x816 == x513`` gate.

The confidence gate (§4 of the design doc):

* **Primary balance:** ``x360 == x435 + (x513 or 0)`` (assets == equity +
  liabilities) within tolerance. Mismatch → the whole filing is untrustworthy:
  ``unbalanced=True``, no values.
* **Assets decomposition:** ``x376 + x424 == x360`` (``x376`` non-current assets
  **may be negative** — housing companies — so there is no positivity check).
  On mismatch the two asset components are suppressed (the gate-verified total
  ``x360`` still stands).
* **P&L waterfall (two legs):** Leg 1 — when both ``x12`` (net financial items)
  and ``x738`` are present: ``|x689 + x12 − x738| ≤ tol``; failure suppresses
  ``net_income``. Leg 2 — when ``x738`` is present: ``|x738 + (x541 or 0) −
  x740| ≤ tol``; failure suppresses ``net_income``. When ``x738`` is absent both
  legs are skipped and ``x740`` is emitted directly. Never ``x738``.

Always suppressed (semantics unconfirmed under FAS): ``income_tax`` (tax is
routed through appropriations, not a clean line), ``cash`` (``x438`` ambiguous),
``financial_debt`` (no reliable bank-borrowings vs trade-payables split), and
``provisions`` (no confirmed code).
"""
from __future__ import annotations

from ._common import _tol

# ---------------------------------------------------------------------------
# The validated concept pack: curated key -> fi_MC metric code. Every entry is
# reconciled to the cent on the three real fixtures. ``interest_expense`` is the
# absolute value of x4046 (always stored negative); ``net_income`` (x740) and the
# asset components (x376/x424) carry extra gate logic below.
# ---------------------------------------------------------------------------
FI_PACK: dict[str, int] = {
    "revenue":            673,
    "operating_income":   689,   # canonical engine key (was operating_profit)
    "net_income":         740,   # FINAL — after appropriations; never x738
    "interest_expense":  4046,   # abs()
    "assets":             360,   # canonical engine key (was total_assets)
    "equity":             435,
    "liabilities":        513,
    "personnel_costs":   1869,
    "non_current_assets": 376,   # may be negative
    "assets_current":     424,   # canonical engine key (was current_assets)
}

# Concepts we never emit for FI, with the reason recorded in ``suppressed``.
_ALWAYS_SUPPRESS: dict[str, str] = {
    "income_tax": "FAS routes tax through appropriations (x541); no confirmed "
                  "income-tax line (x448 is not it) — suppressed (no false data)",
    "cash": "x438 cash semantics are ambiguous under FAS — suppressed (no false data)",
    "financial_debt": "no reliable bank-borrowings vs trade-payables split — "
                      "liabilities-based leverage only — suppressed",
    "provisions": "no confirmed provisions code on FAS filings — suppressed",
}

# Codes handled by dedicated gate logic rather than the plain pack loop.
_GATED = {"net_income", "non_current_assets", "assets_current"}


def map_fi_facts(parsed: dict) -> dict:
    """One ``parse_fi_facts`` result -> curated financials for the current period.

    ``parsed`` is ``{"period_end", "currency", "fields": {mcy_int: float}}``.
    Returns ``{period_end, basis, currency, values, suppressed, unbalanced}``:
    ``basis`` is ``"company"``; ``values[key] = {"value", "unit":"EUR", "label",
    "tag":"fi_MC:xNNN"}``; ``suppressed`` is a list of ``(key, reason)``;
    ``unbalanced`` is True when the primary balance gate fails (then ``values``
    is empty).
    """
    fields: dict[int, float] = parsed.get("fields") or {}
    period_end = parsed.get("period_end")
    currency = parsed.get("currency") or "EUR"

    values: dict[str, dict] = {}
    suppressed: list[tuple[str, str]] = []

    def emit(key: str, code: int, value: float) -> None:
        values[key] = {"value": value, "unit": "EUR", "label": key,
                       "tag": f"fi_MC:x{code}"}

    def suppress(key: str, reason: str) -> None:
        suppressed.append((key, reason))

    def result(unbalanced: bool) -> dict:
        return {
            "period_end": period_end, "basis": "company", "currency": currency,
            "values": values, "suppressed": suppressed, "unbalanced": unbalanced,
        }

    x360 = fields.get(360)   # total assets
    x435 = fields.get(435)   # equity
    x513 = fields.get(513)   # total liabilities (absent -> 0 for zero-debt filers)

    # --- Primary balance gate: assets == equity + liabilities. On mismatch the
    #     whole filing is untrustworthy -> unbalanced, emit NO values (a wrong
    #     balance sheet is worse than none). When x360 or x435 is absent the gate
    #     cannot run; we proceed (directly-tagged values still stand), mirroring
    #     the BE/UK siblings.
    if x360 is not None and x435 is not None:
        scale = max(abs(x360), abs(x435), abs(x513 or 0.0))
        if abs(x360 - (x435 + (x513 or 0.0))) > _tol(scale):
            suppress("__all__",
                     f"unbalanced: total assets x360 {x360} != equity x435 {x435}"
                     f" + liabilities x513 {x513 or 0.0}")
            return result(unbalanced=True)

    # --- Plain confirmed pack: directly-tagged value or a recorded absence.
    for key, code in FI_PACK.items():
        if key in _GATED:
            continue
        raw = fields.get(code)
        if raw is None:
            suppress(key, f"fi_MC:x{code} absent on the filing")
        elif key == "interest_expense":
            emit(key, code, abs(raw))        # x4046 stored negative
        else:
            emit(key, code, raw)

    # --- net_income (x740) guarded by the two-leg appropriations waterfall (NEVER x738).
    # Leg 1 (when x12 net financial items and x738 both present): x689 + x12 == x738.
    # Leg 2 (when x738 present): x738 + (x541 or 0) == x740.
    x689 = fields.get(689)   # operating income — also emitted above via the plain loop
    x12  = fields.get(12)    # net financial items
    x738, x541, x740 = fields.get(738), fields.get(541), fields.get(740)
    if x740 is None:
        suppress("net_income",
                 "fi_MC:x740 (final result after appropriations) absent")
    elif (x12 is not None and x738 is not None
          and abs((x689 or 0.0) + x12 - x738)
              > _tol(max(abs(x689 or 0.0), abs(x738)))):
        suppress("net_income",
                 f"P&L leg-1 fails: x689 {x689} + x12 {x12} != x738 "
                 f"{x738} — P&L is untrusted, net_income suppressed "
                 "(never falls back to x738)")
    elif x738 is not None and abs(x738 + (x541 or 0.0) - x740) > _tol(max(abs(x738), abs(x740))):
        suppress("net_income",
                 f"P&L leg-2 fails: x738 {x738} + x541 {x541 or 0.0} != x740 "
                 f"{x740} — net_income unconfirmed (never falls back to x738)")
    else:
        emit("net_income", 740, x740)

    # --- Asset components. x376 (non-current) MAY be negative — no positivity
    #     check. Cross-checked by the decomposition x376 + x424 == x360 only when
    #     both components are present; on mismatch BOTH are suppressed (the
    #     gate-verified total x360 still stands).
    x376, x424 = fields.get(376), fields.get(424)
    decomp_fails = (
        x376 is not None and x424 is not None and x360 is not None
        and abs((x376 + x424) - x360) > _tol(abs(x360))
    )
    for key, code, raw in (("non_current_assets", 376, x376),
                           ("assets_current", 424, x424)):
        if raw is None:
            suppress(key, f"fi_MC:x{code} absent on the filing")
        elif decomp_fails:
            suppress(key,
                     f"asset decomposition fails: x376 + x424 "
                     f"({(x376 or 0.0) + (x424 or 0.0)}) != total assets x360 "
                     f"({x360}) beyond tol — components unconfirmed")
        else:
            emit(key, code, raw)             # x376 negative is accepted as-is

    # --- Leverage (liabilities-based). The x583/x816 maturity split reconciles
    #     to x513 but its long/short LABEL is unconfirmable from the instance
    #     (no label linkbase; external taxonomy). We never guess which is long vs
    #     short, so the engine's long_term_debt/short_term_debt are suppressed;
    #     the confirmed TOTAL liabilities (x513, emitted above) carries the
    #     liabilities-based leverage. The reason records the most specific cause.
    x583, x816 = fields.get(583), fields.get(816)
    if x513 is None:
        lev_reason = ("no total liabilities (x513) on the filing — "
                      "liabilities-based leverage not applicable")
    elif x583 is None and x816 is None:
        lev_reason = ("no maturity buckets (x583/x816) present — "
                      "no leverage split to emit")
    elif abs((x583 or 0.0) + (x816 or 0.0) - x513) > _tol(abs(x513)):
        lev_reason = (f"x583+x816 ({(x583 or 0.0) + (x816 or 0.0)}) != total "
                      f"liabilities x513 ({x513}) beyond tol — maturity split "
                      "does not reconcile; suppressed")
    else:
        lev_reason = ("x583/x816 reconcile to x513 but the long-term vs "
                      "short-term label is UNCONFIRMED from the instance "
                      "(no label linkbase) — never guess; maturity split "
                      "suppressed; no total_debt/debt_to_equity produced "
                      "(a codelist confirming the split would re-enable it)")
    suppress("long_term_debt", lev_reason)
    suppress("short_term_debt", lev_reason)

    # --- Concepts we never emit for FI (FAS semantics unconfirmed).
    for key, reason in _ALWAYS_SUPPRESS.items():
        suppress(key, reason)

    return result(unbalanced=False)
