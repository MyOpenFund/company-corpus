"""Bare-iXBRL Arelle loader for UK Companies House accounts.

Parses a local iXBRL ``.html`` accounts file (as filed at Companies House) into
an OIM xBRL-JSON dict — the same shape ``flatten_oim_json`` consumes.

Mirrors ``company_corpus/eu/arelle_esef.py`` almost verbatim; the only
difference is that Companies House supplies a bare ``.html`` file (inline-XBRL
with the FRC taxonomy), not a report-package ``.zip``, so we load the file
directly without any zip unpacking or inner-report lookup.

Arelle is an OPTIONAL dependency: pip install '.[eu-financials]'.
"""
from __future__ import annotations

_OIM_DOCTYPE = "https://xbrl.org/2021/xbrl-json"


def oim_from_ch_html(html_path: str, *, cntlr=None) -> dict:
    """A UK Companies House iXBRL ``.html`` file -> an OIM xBRL-JSON dict.

    Pass a shared Arelle ``cntlr`` to amortise the one-time taxonomy-load cost
    across many files. Raises ``ImportError`` (with an install hint) if Arelle
    is absent; ``ValueError`` if the file yields no facts.
    """
    try:
        from arelle import Cntlr
    except ImportError as exc:  # optional dependency
        raise ImportError(
            "UK Companies House iXBRL parsing needs Arelle — install the optional "
            "extra: pip install '.[eu-financials]'"
        ) from exc

    own = cntlr is None
    if own:
        cntlr = Cntlr.Cntlr(logFileName="logToBuffer")
    try:
        cntlr.webCache.noCertificateCheck = True   # tolerate SSL-inspection proxies
    except Exception:  # noqa: BLE001
        pass

    model = None
    try:
        model = cntlr.modelManager.load(html_path)  # bare file, no zip / inner-report path
        if model is None or not getattr(model, "facts", None):
            raise ValueError(f"Arelle parsed no facts from {html_path}")
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
            concept = f"{q.prefix}:{q.localName}" if q.prefix else q.localName
            dims: dict = {"concept": concept, "period": period}
            if f.unit is not None and f.unit.measures and f.unit.measures[0]:
                num = str(f.unit.measures[0][0])
                den = f.unit.measures[1]
                dims["unit"] = f"{num}/{den[0]}" if den else num   # e.g. "iso4217:GBP/xbrli:shares"
            for dq in getattr(ctx, "qnameDims", {}) or {}:          # segment dims
                dims[str(dq)] = "segment"
            facts[f"f{i}"] = {"value": str(f.value), "decimals": f.decimals, "dimensions": dims}
        return {"documentInfo": {"documentType": _OIM_DOCTYPE}, "facts": facts}
    finally:
        if model is not None:
            model.close()
        if own:
            cntlr.close()
