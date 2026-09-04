"""Luxembourg eCDF-XML parser — stdlib only (xml.etree.ElementTree).

Parses the STATEC/LBR eCDF XML format produced by the Luxembourg Business
Registers (LBR) and distributed via data.public.lu.

Public API
----------
parse_lu_declarers(source) -> list[dict]
    source : path (str/Path) or raw bytes of an eCDF XML file.
    Returns one dict per <Declarer> element.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import Union


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_raw(source: Union[str, bytes, os.PathLike]) -> bytes:
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as f:
            return f.read()
    return bytes(source)


def _parse_root(source: Union[str, bytes, os.PathLike]) -> ET.Element:
    """Read source and return the parsed XML root element.

    Handles both UTF-8 and ISO-8859-15 encoded files by inspecting the XML
    declaration before decoding.
    """
    raw = _load_raw(source)
    m = re.match(rb'<\?xml[^>]*encoding=["\']([^"\']+)["\']', raw)
    enc = m.group(1).decode("ascii") if m else "utf-8"
    utf8 = raw.decode(enc).encode("utf-8")
    if m:
        utf8 = re.sub(rb'encoding=["\'][^"\']+["\']', b'encoding="UTF-8"', utf8, count=1)
    return ET.fromstring(utf8)


def _collect_fields(form_data: ET.Element) -> dict[int, float]:
    """Walk the <FormData> field tree and return {ecdf_int: float}.

    Fields are hierarchically nested; the outermost (first-encountered in
    document order) occurrence of each ecdf code is the section total — that
    is the value we keep.  Inner repeated occurrences are ignored.
    Empty or missing <Data> elements are skipped.
    """
    fields: dict[int, float] = {}
    seen: set[int] = set()

    def _walk(elem: ET.Element) -> None:
        if elem.tag == "Field":
            raw_ecdf = elem.get("ecdf")
            if raw_ecdf is not None:
                ecdf = int(raw_ecdf)
                if ecdf not in seen:
                    seen.add(ecdf)
                    data_el = elem.find("Data")
                    if data_el is not None and data_el.text and data_el.text.strip():
                        fields[ecdf] = float(data_el.text.strip())
        for child in elem:
            _walk(child)

    _walk(form_data)
    return fields


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_lu_declarers(
    source: Union[str, bytes, os.PathLike],
) -> list[dict]:
    """Parse a Luxembourg eCDF XML file or bytes.

    Parameters
    ----------
    source:
        File path (str or Path) **or** raw bytes of an eCDF XML document.

    Returns
    -------
    list[dict]
        One dict per ``<Declarer>`` with the shape::

            {
                "rcs":  str,          # e.g. "B60814"
                "name": str,          # LegalUnitName
                "declarations": [
                    {
                        "type":       str,         # e.g. "CA_BILAN"
                        "model":      str,         # taxonomy version
                        "currency":   str,         # e.g. "EUR"
                        "period_end": str | None,  # "YYYY-MM-DD"
                        "fields":     {int: float},# {ecdf_code: value}
                    }
                ]
            }
    """
    root = _parse_root(source)
    declarers: list[dict] = []

    for decl_el in root.iter("Declarer"):
        rcs_el = decl_el.find("RcsNumber")
        name_el = decl_el.find("LegalUnitName")
        rcs = rcs_el.text.strip() if rcs_el is not None and rcs_el.text else ""
        name = name_el.text.strip() if name_el is not None and name_el.text else ""

        declarations: list[dict] = []
        for dec_el in decl_el.findall("Declaration"):
            dec_type = dec_el.get("type", "")
            dec_model = dec_el.get("model", "")

            cur_el = dec_el.find("Currency")
            currency = (
                cur_el.text.strip()
                if cur_el is not None and cur_el.text
                else ""
            )

            end_el = dec_el.find("EndDate")
            period_end = (
                end_el.text.strip()
                if end_el is not None and end_el.text
                else None
            )

            form_data = dec_el.find("FormData")
            fields = _collect_fields(form_data) if form_data is not None else {}

            declarations.append(
                {
                    "type": dec_type,
                    "model": dec_model,
                    "currency": currency,
                    "period_end": period_end,
                    "fields": fields,
                }
            )

        declarers.append({"rcs": rcs, "name": name, "declarations": declarations})

    return declarers
