"""Map a Brønnøysund (Brreg) regnskap JSON entry to our curated financial concepts.

Brreg serves structured JSON with named numeric fields (not XBRL), so this is a
direct field->key mapping with fallbacks, over a flatten of the three accounts
blocks (robust to nesting). Leverage is liabilities-based (NGAAP gives total
liabilities, not pure borrowings) — see docs/REGISTER_FINANCIALS.md.
"""
from __future__ import annotations

from datetime import date

# curated key -> Brreg leaf field name(s), highest priority first.
NO_FIELDS: dict[str, tuple[str, ...]] = {
    "revenue": ("sumDriftsinntekter", "salgsinntekter"),
    "operating_income": ("driftsresultat",),
    "pretax_income": ("ordinaertResultatFoerSkattekostnad",),
    "income_tax": ("ordinaertResultatSkattekostnad",),
    "net_income": ("aarsresultat",),
    # Gross "other" interest only. NOT `sumFinanskostnad` as a fallback: that is a
    # NET/aggregate financial figure (207M in the real data — *smaller* than
    # annenRentekostnad's 1379M, and equal to |nettoFinans|), not gross interest
    # expense, so it would corrupt interest_coverage. Intra-group interest
    # (`rentekostnadSammeKonsern`) is not summed in either, so interest_coverage is an
    # approximation that excludes intra-group interest.
    "interest_expense": ("annenRentekostnad",),
    "assets": ("sumEiendeler",),
    "assets_current": ("sumOmloepsmidler",),
    "cash": ("sumBankinnskuddOgKontanter",),
    "inventory": ("sumVarer",),
    "receivables": ("sumFordringer",),
    "equity": ("sumEgenkapital",),
    "equity_total": ("sumEgenkapital",),
    "liabilities": ("sumGjeld",),
    "liabilities_current": ("sumKortsiktigGjeld",),
    "short_term_debt": ("sumKortsiktigGjeld",),   # so total_debt ~= total liabilities (NGAAP gearing)
    "long_term_debt": ("sumLangsiktigGjeld",),
}

_BASIS = {"KONSERN": "consolidated", "SELSKAP": "company"}
_BLOCKS = ("resultatregnskapResultat", "eiendeler", "egenkapitalGjeld")


def _leaves(obj) -> dict[str, float]:
    """Recursively collect numeric leaves by field name (last-wins on duplicates)."""
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[k] = v
            elif isinstance(v, (dict, list)):
                out.update(_leaves(v))
    elif isinstance(obj, list):
        for v in obj:
            out.update(_leaves(v))
    return out


def map_brreg_entry(entry: dict) -> dict | None:
    """One Brreg regnskap entry -> {period_end, basis, currency, values}; None if unusable."""
    period = (entry.get("regnskapsperiode") or {}).get("tilDato")
    if not period:
        return None
    try:  # a non-ISO tilDato (e.g. "31.12.2024") skips THIS entry, not the whole batch
        date.fromisoformat(period)
    except ValueError:
        return None
    flat: dict[str, float] = {}
    for block in _BLOCKS:
        flat.update(_leaves(entry.get(block) or {}))
    currency = (entry.get("valuta") or "").upper() or "NOK"
    basis = _BASIS.get(entry.get("regnskapstype", ""), "company")
    values: dict[str, dict] = {}
    for key, fields in NO_FIELDS.items():
        for fld in fields:
            if fld in flat:
                values[key] = {"value": flat[fld], "unit": currency, "label": key, "tag": fld}
                break
    # Brreg omits sumLangsiktigGjeld when a filer has no non-current liabilities
    # (langsiktigGjeld: {}), but the engine gates total_debt — and every gearing metric —
    # on long_term_debt being present. Synthesize it from total − current liabilities so
    # gearing computes for the small/private filers this pillar targets. No-op when
    # sumLangsiktigGjeld is present (long_term_debt already set; there Lang == total − current).
    if "long_term_debt" not in values and "liabilities" in values:
        cur = (values.get("liabilities_current") or {}).get("value", 0) or 0
        values["long_term_debt"] = {
            "value": values["liabilities"]["value"] - cur, "unit": currency,
            "label": "long_term_debt", "tag": "sumGjeld−sumKortsiktigGjeld (derived)"}
    if not values:
        return None
    # Balance gate: assets == equity + liabilities within tolerance.
    # Brreg provides all three fields (sumEiendeler / sumEgenkapital / sumGjeld) so the
    # check is cheap and guards against malformed submissions reaching the engine.
    assets_v = (values.get("assets") or {}).get("value")
    equity_v = (values.get("equity") or {}).get("value")
    liab_v = (values.get("liabilities") or {}).get("value")
    unbalanced = False
    suppressed: list = []
    if assets_v is not None and equity_v is not None and liab_v is not None:
        tol = max(2.0, 0.005 * abs(assets_v))
        if abs(assets_v - (equity_v + liab_v)) > tol:
            unbalanced = True
            suppressed = [("__all__", "assets != equity+liabilities")]
            values = {}
    return {"period_end": period, "basis": basis, "currency": currency,
            "values": values, "suppressed": suppressed, "unbalanced": unbalanced}
