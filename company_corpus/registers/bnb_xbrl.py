"""Belgium BNB CBSO dimensional-XBRL parser — stdlib only, no Arelle.

Parses a BNB *-data.xbrl file (XBRL instance document) into a flat list of
monetary facts, each carrying the context's dimension members plus the numeric
value and unit (e.g. EUR, shares, pure).

Public API
----------
parse_bnb_data_xbrl(source) -> list[dict]
    source: path (str/Path) **or** raw bytes of a -data.xbrl file.
    Returns one dict per monetary fact (has unitRef, not xsi:nil):
        {"dims": {dim_local: member_local}, "value": float, "unit": str}

open_bnb_deposit(zip_bytes: bytes) -> bytes
    A BNB deposit zip contains three files (*-contact.xbrl, *-data.xbrl,
    *-vendor.xbrl).  Returns the bytes of the *-data.xbrl member.
"""
from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Union

# XBRL / XBRLDI namespace URIs
_NS_XBRLI = "http://www.xbrl.org/2003/instance"
_NS_XBRLDI = "http://xbrl.org/2006/xbrldi"
_NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"

_TAG_CONTEXT = f"{{{_NS_XBRLI}}}context"
_TAG_PERIOD = f"{{{_NS_XBRLI}}}period"
_TAG_END_DATE = f"{{{_NS_XBRLI}}}endDate"
_TAG_INSTANT = f"{{{_NS_XBRLI}}}instant"
_TAG_SCENARIO = f"{{{_NS_XBRLI}}}scenario"
_TAG_EXPLICIT = f"{{{_NS_XBRLDI}}}explicitMember"
_ATTR_NIL = f"{{{_NS_XSI}}}nil"

# Tags that are part of the XBRL instance boilerplate — not data facts
_SKIP_LOCAL = {"context", "unit", "schemaRef"}


def _local(tag: str) -> str:
    """Return the local part of a Clark-notation tag or a prefixed QName."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag.split(":", 1)[-1]


def _build_context_index(root: ET.Element) -> dict[str, dict[str, str]]:
    """Return {ctx_id: {dim_local: member_local}} for every xbrli:context."""
    index: dict[str, dict[str, str]] = {}
    for ctx in root.iter(_TAG_CONTEXT):
        ctx_id = ctx.get("id", "")
        dims: dict[str, str] = {}
        scenario = ctx.find(_TAG_SCENARIO)
        if scenario is not None:
            for member in scenario.iter(_TAG_EXPLICIT):
                dim_qname = member.get("dimension", "")   # e.g. "dim:bas"
                mem_text = (member.text or "").strip()    # e.g. "bas:m25"
                dim_local = dim_qname.split(":")[-1]      # "bas"
                mem_local = mem_text.split(":")[-1]       # "m25"
                if dim_local and mem_local:
                    dims[dim_local] = mem_local
        index[ctx_id] = dims
    return index


def parse_bnb_data_xbrl(source: Union[str, bytes, Path]) -> list[dict]:
    """Parse a BNB -data.xbrl and return one dict per monetary fact.

    Parameters
    ----------
    source:
        A file path (str or Path) or raw bytes of the -data.xbrl document.

    Returns
    -------
    list of {"dims": dict, "value": float, "unit": str}
        Only facts that carry a ``unitRef`` attribute are included.
        Facts with ``xsi:nil="true"`` are silently skipped.
    """
    if isinstance(source, (str, Path)):
        tree = ET.parse(str(source))
        root = tree.getroot()
    else:
        root = ET.fromstring(source)

    ctx_index = _build_context_index(root)

    facts: list[dict] = []
    for elem in root:
        local = _local(elem.tag)
        if local in _SKIP_LOCAL:
            continue

        ctx_ref = elem.get("contextRef")
        if ctx_ref is None:
            continue  # not a fact

        # Skip nil facts
        if elem.get(_ATTR_NIL, "").lower() == "true":
            continue

        unit_ref = elem.get("unitRef")
        if unit_ref is None:
            continue  # non-monetary / string / date / boolean fact

        text = (elem.text or "").strip()
        if not text:
            continue

        try:
            value = float(text)
        except ValueError:
            continue  # shouldn't happen for numeric unitRef elements, but be safe

        dims = dict(ctx_index.get(ctx_ref, {}))
        facts.append({"dims": dims, "value": value, "unit": unit_ref})

    return facts


def _period_end_from_root(root: ET.Element) -> "str | None":
    """Extract the max period-end date from an already-parsed XBRL root element.

    Iterates every ``xbrli:context/xbrli:period`` and collects all ``endDate``
    and ``instant`` text values.  Each string is normalised to a plain
    ``date`` via ``date.fromisoformat(s.strip()[:10])`` — robust against
    timestamps with a trailing ``T00:00:00`` — and the max ``date`` is returned
    as ``"YYYY-MM-DD"``.  Strings that cannot be parsed are silently skipped.
    Returns ``None`` when no valid period date is found.
    """
    parsed: list[date] = []
    for ctx in root.iter(_TAG_CONTEXT):
        period = ctx.find(_TAG_PERIOD)
        if period is None:
            continue
        for tag in (_TAG_END_DATE, _TAG_INSTANT):
            el = period.find(tag)
            if el is not None and el.text and el.text.strip():
                try:
                    parsed.append(date.fromisoformat(el.text.strip()[:10]))
                except ValueError:
                    pass  # skip malformed / non-ISO strings
    return max(parsed).isoformat() if parsed else None


def period_end_of(source: Union[str, bytes, Path]) -> "str | None":
    """Return the filing's reporting date as the max of all context period dates.

    Iterates every ``xbrli:context/xbrli:period`` in the instance document and
    returns the maximum ``endDate`` or ``instant`` as an ISO-8601 string
    (``"YYYY-MM-DD"``).  Returns ``None`` when no dated period is found.

    For a BNB filing the ``prd`` dimension selects current vs prior year *within*
    one exercise date, so all dated periods resolve to the same end date (the
    filing's reporting date).  Taking the max is therefore equivalent to reading
    any one of them, but is robust against filings that might contain multiple
    distinct dates.

    Parameters
    ----------
    source:
        A file path (str or Path) or raw bytes of the -data.xbrl document.

    Returns
    -------
    str or None
        The max reporting date as ``"YYYY-MM-DD"``, or ``None``.
    """
    if isinstance(source, (str, Path)):
        tree = ET.parse(str(source))
        root = tree.getroot()
    else:
        root = ET.fromstring(source)

    return _period_end_from_root(root)


def parse_bnb_document(
    source: Union[str, bytes, Path],
) -> "tuple[list[dict], str | None]":
    """Parse a BNB -data.xbrl **once** and return ``(facts, period_end)``.

    Combines the work of :func:`parse_bnb_data_xbrl` and
    :func:`period_end_of` into a single XML parse.  Use this on the producer
    path (``_be_pipeline``) to avoid parsing large documents twice per filing
    at batch scale.

    The existing :func:`parse_bnb_data_xbrl` and :func:`period_end_of`
    functions are unchanged — they each still parse independently and their
    own tests continue to pass.

    Parameters
    ----------
    source:
        A file path (str or Path) or raw bytes of the -data.xbrl document.

    Returns
    -------
    (facts, period_end)
        *facts* — same list as :func:`parse_bnb_data_xbrl`.
        *period_end* — same ``str | None`` as :func:`period_end_of`.
    """
    if isinstance(source, (str, Path)):
        tree = ET.parse(str(source))
        root = tree.getroot()
    else:
        root = ET.fromstring(source)

    # --- facts (same logic as parse_bnb_data_xbrl) ---
    ctx_index = _build_context_index(root)
    facts: list[dict] = []
    for elem in root:
        local = _local(elem.tag)
        if local in _SKIP_LOCAL:
            continue
        ctx_ref = elem.get("contextRef")
        if ctx_ref is None:
            continue
        if elem.get(_ATTR_NIL, "").lower() == "true":
            continue
        unit_ref = elem.get("unitRef")
        if unit_ref is None:
            continue
        text = (elem.text or "").strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            continue
        dims = dict(ctx_index.get(ctx_ref, {}))
        facts.append({"dims": dims, "value": value, "unit": unit_ref})

    # --- period_end (via shared helper, robust date parsing) ---
    period_end = _period_end_from_root(root)

    return facts, period_end


def open_bnb_deposit(zip_bytes: bytes) -> bytes:
    """Extract the *-data.xbrl member from a BNB deposit zip.

    A BNB deposit zip contains exactly three files:
        *-contact.xbrl, *-data.xbrl, *-vendor.xbrl

    Parameters
    ----------
    zip_bytes:
        Raw bytes of the deposit .zip file.

    Returns
    -------
    bytes
        The raw bytes of the *-data.xbrl member.

    Raises
    ------
    KeyError
        If no member whose name ends with ``-data.xbrl`` is found.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.endswith("-data.xbrl"):
                return zf.read(name)
    raise KeyError("No *-data.xbrl member found in BNB deposit zip")
