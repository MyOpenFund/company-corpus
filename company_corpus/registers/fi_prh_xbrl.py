"""
stdlib dimensional XBRL parser for Finnish PRH (Patent and Registration Office).

PRH publishes XBRL instance documents with a dimensional model: each numeric
fact is stored in an element of the ``fi_met`` namespace whose ``contextRef``
attribute points to a context that carries the metric code as the MCY dimension
member (e.g. ``fi_MC:x673`` → MCY integer 673 = revenue).

No Arelle dependency — pure ``xml.etree.ElementTree``.

Usage::

    from company_corpus.registers.fi_prh_xbrl import parse_fi_facts

    result = parse_fi_facts("path/to/filing.xml")
    # result == {"period_end": "2024-12-31", "currency": "EUR", "fields": {673: 481773.33, ...}}
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Union

# ---------------------------------------------------------------------------
# Namespace constants (Clark notation)
# ---------------------------------------------------------------------------
_NS_XBRLI = "http://www.xbrl.org/2003/instance"
_NS_XBRLDI = "http://xbrl.org/2006/xbrldi"
_NS_MET = "http://www.suomi.fi/xbrl/crr/dict/met"

_CTX_TAG = f"{{{_NS_XBRLI}}}context"
_INSTANT_TAG = f"{{{_NS_XBRLI}}}instant"
_SCENARIO_TAG = f"{{{_NS_XBRLI}}}scenario"
_MEMBER_TAG = f"{{{_NS_XBRLDI}}}explicitMember"
_MET_PREFIX = f"{{{_NS_MET}}}"

# Extract the integer from e.g. "fi_MC:x673" → 673
_MCY_RE = re.compile(r":x(\d+)$")

# Map XBRL unit IDs to ISO currency codes
_UNIT_MAP: dict[str, str] = {
    "ISO4217_EUR": "EUR",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_fi_facts(
    source: Union[str, bytes, Path],
) -> dict:
    """Parse a PRH XBRL instance and return current-period dimensional facts.

    Parameters
    ----------
    source:
        Path (``str`` or ``pathlib.Path``) to the XBRL file, or raw ``bytes``
        of its content.

    Returns
    -------
    dict
        ``{"period_end": str | None, "currency": str, "fields": {mcy_int: float}}``

        * ``period_end``  — ``"YYYY-MM-DD"`` reporting instant of the current
          period, or ``None`` if not determinable.
        * ``currency``    — ISO currency code (typically ``"EUR"``).
        * ``fields``      — mapping of MCY dimension integers to numeric values,
          current period only (contexts that carry a ``fi_dim:REF`` member are
          classified as prior period and excluded).
    """
    # Parse XML
    if isinstance(source, (str, Path)):
        tree = ET.parse(source)
        root = tree.getroot()
    else:
        root = ET.fromstring(source)

    # ------------------------------------------------------------------ #
    # Step 1 — Build context map                                           #
    # {ctx_id: {"mcy": int|None, "is_prior": bool, "instant": str|None}} #
    # ------------------------------------------------------------------ #
    ctx_map: dict[str, dict] = {}

    for ctx in root.iter(_CTX_TAG):
        ctx_id = ctx.get("id")
        if ctx_id is None:
            continue

        # Reporting instant (may be absent for duration contexts, but PRH uses instant)
        instant_el = ctx.find(f".//{_INSTANT_TAG}")
        instant: str | None = (
            instant_el.text.strip() if instant_el is not None and instant_el.text else None
        )

        # Scenario → MCY member + prior-period flag
        mcy: int | None = None
        is_prior = False
        scenario = ctx.find(_SCENARIO_TAG)
        if scenario is not None:
            for member in scenario.findall(_MEMBER_TAG):
                dim = member.get("dimension", "")
                value = (member.text or "").strip()

                # MCY dimension carries the metric code (e.g. fi_MC:x673)
                if "MCY" in dim:
                    m = _MCY_RE.search(value)
                    if m:
                        mcy = int(m.group(1))

                # Any REF dimension member marks this as a prior/comparative period
                if "REF" in dim:
                    is_prior = True

        ctx_map[ctx_id] = {"mcy": mcy, "is_prior": is_prior, "instant": instant}

    # ------------------------------------------------------------------ #
    # Step 2 — Collect numeric facts from the met namespace               #
    # ------------------------------------------------------------------ #
    period_end: str | None = None
    currency = "EUR"
    fields: dict[int, float] = {}

    for elem in root.iter():
        if not elem.tag.startswith(_MET_PREFIX):
            continue

        ctx_ref = elem.get("contextRef")
        unit_ref = elem.get("unitRef")
        text = (elem.text or "").strip()

        # Metadata elements (company name, filing dates) carry no unitRef
        if not ctx_ref or not unit_ref or not text:
            continue

        ctx = ctx_map.get(ctx_ref)
        if ctx is None:
            continue

        # Resolve currency from unit reference
        if unit_ref in _UNIT_MAP:
            currency = _UNIT_MAP[unit_ref]
        elif unit_ref.startswith("ISO4217_"):
            currency = unit_ref[8:]

        # Skip metadata contexts (no MCY) and prior-period contexts
        if ctx["mcy"] is None or ctx["is_prior"]:
            continue

        # Parse the numeric value
        try:
            fields[ctx["mcy"]] = float(text)
        except ValueError:
            continue

        # Use the instant from the first current fact as the reporting period end
        if period_end is None and ctx["instant"]:
            period_end = ctx["instant"]

    return {"period_end": period_end, "currency": currency, "fields": fields}
