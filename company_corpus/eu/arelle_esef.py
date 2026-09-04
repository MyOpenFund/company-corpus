"""Tier B — parse a local ESEF report-package .zip into an OIM xBRL-JSON dict using
Arelle (the same shape filings.xbrl.org's json_url returns, so flatten_oim_json
consumes it). Arelle is an OPTIONAL dependency: pip install '.[eu-financials]'.
"""
from __future__ import annotations

import zipfile

_OIM_DOCTYPE = "https://xbrl.org/2021/xbrl-json"


def oim_from_esef_zip(zip_path: str, *, cntlr=None, config=None) -> dict:
    """An ESEF report-package .zip -> an OIM xBRL-JSON dict.

    Pass a shared Arelle ``cntlr`` to amortize the one-time IFRS-taxonomy load
    across many zips. Raises ImportError (with an install hint) if Arelle is
    absent; ValueError if the package has no inline-XBRL report or yields no facts.

    NB the input must be the report-package zip (which bundles the issuer's
    extension taxonomy), not a bare ``.xhtml`` report — a standalone inline-XBRL
    document can't resolve its extension concepts, so Arelle yields no facts.
    """
    try:
        from arelle import Cntlr
    except ImportError as exc:  # optional dependency
        raise ImportError(
            "Tier B ESEF parsing needs Arelle — install the optional extra: "
            "pip install '.[eu-financials]'"
        ) from exc

    with zipfile.ZipFile(zip_path) as zf:
        inner = [n for n in zf.namelist()
                 if n.lower().endswith(".xhtml") and "/reports/" in n.lower()]
    if not inner:
        raise ValueError(f"no inline-XBRL report (reports/*.xhtml) in {zip_path}")

    own = cntlr is None
    if own:
        cntlr = Cntlr.Cntlr(logFileName="logToBuffer")
    # Only disable TLS verification when the caller explicitly opts out (e.g. behind
    # a trusted SSL-inspection proxy).  Default is always to verify — mirrors Fetcher.
    if not getattr(config, "verify_tls", True):
        try:
            cntlr.webCache.noCertificateCheck = True
        except Exception:  # noqa: BLE001
            pass

    model = None
    try:
        model = cntlr.modelManager.load(f"{zip_path}/{inner[0]}")  # the inner report, not the zip
        if model is None or not getattr(model, "facts", None):
            raise ValueError(f"Arelle parsed no facts from {zip_path}")
        facts: dict[str, dict] = {}
        for i, f in enumerate(model.facts):
            q, ctx = f.qname, f.context
            if q is None or ctx is None or getattr(f, "isNil", False):
                continue
            if ctx.isInstantPeriod:
                period = ctx.instantDatetime.isoformat()
            elif ctx.isStartEndPeriod:
                period = f"{ctx.startDatetime.isoformat()}/{ctx.endDatetime.isoformat()}"
            else:
                continue
            concept = f"{q.prefix}:{q.localName}" if q.prefix else q.localName  # flatten strips the prefix
            dims: dict = {"concept": concept, "period": period}
            if f.unit is not None and f.unit.measures and f.unit.measures[0]:
                num = str(f.unit.measures[0][0])
                den = f.unit.measures[1]
                dims["unit"] = f"{num}/{den[0]}" if den else num   # e.g. "iso4217:EUR/xbrli:shares"
            for dq in getattr(ctx, "qnameDims", {}) or {}:          # segment dims -> flatten drops these facts
                dims[str(dq)] = "segment"
            facts[f"f{i}"] = {"value": str(f.value), "decimals": f.decimals, "dimensions": dims}
        return {"documentInfo": {"documentType": _OIM_DOCTYPE}, "facts": facts}
    finally:
        if model is not None:
            model.close()
        if own:
            cntlr.close()
