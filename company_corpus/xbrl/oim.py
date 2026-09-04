"""OIM xBRL-JSON (filings.xbrl.org ``json_url``) -> the financials engine's point shape.

Rules (grounded in real filings.xbrl.org reports):
1. Keep only UN-dimensioned facts (drop axis/member breakdowns, restatement members,
   maturity buckets) -> the consolidated top-line, no-guess.
2. Period: instants and duration-ends are canonical midnight (``T00:00:00`` of the day
   AFTER the period) -> shift back one day to the conventional last day, uniformly.
3. Unit: ``iso4217:EUR`` -> ``EUR``; share / per-share -> ``shares`` / ``<ccy>/shares``.
4. Attach the filing's ``filed``/``form``/``accn`` (OIM facts carry none).
5. Values are canonical (``xbrl:canonicalValues``) -> used as-is, ``decimals`` ignored.
"""
from __future__ import annotations

from datetime import date, timedelta

# Dimension keys that are NOT a segment/axis: a fact carrying anything else is a
# disaggregation (by equity component, restatement, maturity, ...), not the total.
_NON_AXIS_DIMS = frozenset({"concept", "period", "unit", "entity", "language"})


def _coerce_value(v):
    """OIM fact values are strings (to preserve precision). Coerce numeric ones to
    int/float so the engine can do arithmetic; leave non-numeric text values as-is."""
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)          # exact for large integers (no float precision loss)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return v            # non-numeric text fact — leave as string
    return v


def normalize_unit(unit: str | None) -> str | None:
    """``iso4217:EUR`` -> ``EUR``; share / per-share ratios -> ``shares`` / ``<ccy>/shares``;
    anything else (``xbrli:pure``, missing) -> None (non-monetary)."""
    if not unit:
        return None
    if "/" in unit:
        num, den = unit.split("/", 1)
        n, d = normalize_unit(num), normalize_unit(den)
        return f"{n}/shares" if (n and d == "shares") else None
    if unit.startswith("iso4217:"):
        return unit.split(":", 1)[1]
    if unit.split(":", 1)[-1] == "shares":
        return "shares"
    return None


def _part(ts: str) -> tuple[date, bool]:
    """(date, is_midnight) for an OIM timestamp ``YYYY-MM-DD`` or ``...T00:00:00[Z]``."""
    d = date.fromisoformat(ts[:10])
    midnight = len(ts) <= 10 or ts[10:].rstrip("Z") in ("", "T00:00:00")
    return d, midnight


def normalize_period(period: str) -> tuple[date | None, date, bool]:
    """Return (start, end, is_instant). A canonical midnight end/instant is shifted
    back one day to the conventional last day of the period."""
    if "/" in period:                       # duration
        s, e = period.split("/", 1)
        sd, _ = _part(s)
        ed, e_mid = _part(e)
        if e_mid:
            ed -= timedelta(days=1)
        return sd, ed, False
    d, mid = _part(period)                   # instant
    if mid:
        d -= timedelta(days=1)
    return None, d, True


def flatten_oim_json(report: dict, *, filed: str, form: str, accn: str) -> dict[str, list[dict]]:
    """One OIM report -> ``{local_concept_name: [engine points]}`` (un-dimensioned only)."""
    out: dict[str, list[dict]] = {}
    for fv in (report.get("facts") or {}).values():
        dims = fv.get("dimensions") or {}
        if any(k not in _NON_AXIS_DIMS for k in dims):   # rule 1: drop disaggregations
            continue
        concept = dims.get("concept")
        period = dims.get("period")
        if not concept or not period:
            continue
        try:
            start, end, instant = normalize_period(period)
        except ValueError:
            continue
        local = concept.split(":", 1)[-1]                # "ifrs-full:Revenue" -> "Revenue"
        unit = normalize_unit(dims.get("unit"))
        point: dict = {
            "val": _coerce_value(fv.get("value")), "end": end.isoformat(),
            "unit": unit if unit is not None else "",
            "tag": local, "label": local,
            "filed": filed, "form": form, "accn": accn,
        }
        if not instant and start is not None:
            point["start"] = start.isoformat()
        out.setdefault(local, []).append(point)
    return out
