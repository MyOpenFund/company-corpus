"""Parsers + summaries for ownership filings (family E).

Form 3/4/5 and 13F are **structured XML**, so their raw text extracts poorly. As
with XBRL financials, we parse them into readable, queryable summaries:

* **Form 3/4/5** (`<ownershipDocument>`) → insider, role, and transactions
  (date, code, shares, price, acquired/disposed, resulting holding).
* **13F-HR** (`<informationTable>`) → holdings (issuer, CUSIP, value, shares) with
  portfolio totals and the largest positions.

SC 13D/G are narrative HTML/text and are left to the generic extraction path.

XML is parsed namespace-insensitively (Form 4 has no namespace; the 13F info
table does).
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date

from .submission import parse_submission


# ---- namespace-insensitive XML helpers ----
def _lname(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _kid(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    for c in el:
        if _lname(c) == name:
            return c
    return None


def _kids(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [c for c in el if _lname(c) == name] if el is not None else []


def _path(el: ET.Element | None, *names: str) -> ET.Element | None:
    cur = el
    for n in names:
        cur = _kid(cur, n)
    return cur


def _text(el: ET.Element | None, *names: str) -> str:
    node = _path(el, *names)
    return node.text.strip() if (node is not None and node.text) else ""


def _extract_root(text: str, tag: str) -> str | None:
    """Slice the ``<tag ...>...</tag>`` element out of surrounding wrappers.

    EDGAR wraps the document XML in an ``<XML>`` tag (and a stray ``<?xml?>``
    declaration), so we cannot parse from the top — locate the real root.
    """
    low, t = text.lower(), tag.lower()
    i = low.find("<" + t)
    j = low.rfind("</" + t + ">")
    if i == -1 or j == -1:
        return None
    return text[i:j + len("</" + t + ">")]


def _parse_root(text: str, tag: str) -> ET.Element | None:
    root_text = _extract_root(text, tag)
    if root_text is None:
        return None
    try:
        return ET.fromstring(root_text)
    except ET.ParseError:
        return None


def find_ownership_doc(submission_raw: str, form_type_code: str) -> str | None:
    """Return the relevant XML document text from a complete submission.

    ``E1`` → the ``<ownershipDocument>`` (Form 3/4/5); ``E2`` → the
    ``<informationTable>`` (13F). Falls back to scanning all documents for the
    marker tag.
    """
    marker = "<ownershipDocument" if form_type_code == "E1" else "<informationTable"
    for doc in parse_submission(submission_raw):
        if marker.lower() in doc.text.lower():
            return doc.text
    # Some 13F info tables wrap rows without the outer tag; match infoTable too.
    if form_type_code == "E2":
        for doc in parse_submission(submission_raw):
            if "<infotable" in doc.text.lower():
                return doc.text
    return None


# ---- Form 3/4/5 ----
@dataclass
class InsiderTxn:
    table: str           # nonDerivative | derivative
    security_title: str
    date: str
    code: str            # transaction code (P=purchase, S=sale, A, M, ...)
    acquired_disposed: str  # A | D
    shares: str
    price: str
    shares_after: str


@dataclass
class InsiderFiling:
    document_type: str
    period_of_report: str
    issuer_name: str
    issuer_cik: str
    issuer_symbol: str
    owner_name: str
    owner_cik: str
    is_officer: bool = False
    officer_title: str = ""
    is_director: bool = False
    is_ten_percent: bool = False
    transactions: list[InsiderTxn] = field(default_factory=list)

    @property
    def role(self) -> str:
        parts = []
        if self.is_director:
            parts.append("Director")
        if self.is_officer:
            parts.append(f"Officer{f' ({self.officer_title})' if self.officer_title else ''}")
        if self.is_ten_percent:
            parts.append("10% owner")
        return ", ".join(parts) or "n/a"


def _txns_from_table(table: ET.Element | None, kind: str) -> list[InsiderTxn]:
    out: list[InsiderTxn] = []
    if table is None:
        return out
    tx_tag = f"{kind}Transaction"
    for tx in _kids(table, tx_tag):
        out.append(InsiderTxn(
            table=kind,
            security_title=_text(tx, "securityTitle", "value"),
            date=_text(tx, "transactionDate", "value"),
            code=_text(tx, "transactionCoding", "transactionCode"),
            acquired_disposed=_text(tx, "transactionAmounts", "transactionAcquiredDisposedCode", "value"),
            shares=_text(tx, "transactionAmounts", "transactionShares", "value"),
            price=_text(tx, "transactionAmounts", "transactionPricePerShare", "value"),
            shares_after=_text(tx, "postTransactionAmounts", "sharesOwnedFollowingTransaction", "value"),
        ))
    return out


def parse_form345(xml_text: str) -> InsiderFiling | None:
    root = _parse_root(xml_text, "ownershipDocument")
    if root is None:
        return None
    rel = _path(root, "reportingOwner", "reportingOwnerRelationship")

    def _flag(name: str) -> bool:
        return _text(rel, name).lower() in ("true", "1")

    filing = InsiderFiling(
        document_type=_text(root, "documentType"),
        period_of_report=_text(root, "periodOfReport"),
        issuer_name=_text(root, "issuer", "issuerName"),
        issuer_cik=_text(root, "issuer", "issuerCik"),
        issuer_symbol=_text(root, "issuer", "issuerTradingSymbol"),
        owner_name=_text(root, "reportingOwner", "reportingOwnerId", "rptOwnerName"),
        owner_cik=_text(root, "reportingOwner", "reportingOwnerId", "rptOwnerCik"),
        is_officer=_flag("isOfficer"),
        officer_title=_text(rel, "officerTitle"),
        is_director=_flag("isDirector"),
        is_ten_percent=_flag("isTenPercentOwner"),
    )
    filing.transactions = (
        _txns_from_table(_kid(root, "nonDerivativeTable"), "nonDerivative")
        + _txns_from_table(_kid(root, "derivativeTable"), "derivative")
    )
    return filing


# ---- 13F-HR ----
@dataclass
class Holding:
    issuer: str
    title_of_class: str
    cusip: str
    value: int
    shares: int
    share_type: str


def parse_13f(xml_text: str) -> tuple[list[Holding], dict]:
    import re

    root = _parse_root(xml_text, "informationTable")
    if root is None:
        # Some info tables ship the rows without the outer <informationTable>.
        parts = re.findall(r"(?is)<infoTable.*?</infoTable>", xml_text)
        root = _parse_root("<informationTable>" + "".join(parts) + "</informationTable>",
                           "informationTable") if parts else None
    holdings: list[Holding] = []
    if root is not None:
        rows = _kids(root, "infoTable") or [el for el in root.iter() if _lname(el) == "infoTable"]
        for r in rows:
            def _int(*names: str) -> int:
                raw = _text(r, *names).replace(",", "")
                try:
                    return int(float(raw)) if raw else 0
                except ValueError:
                    return 0

            holdings.append(Holding(
                issuer=_text(r, "nameOfIssuer"),
                title_of_class=_text(r, "titleOfClass"),
                cusip=_text(r, "cusip"),
                value=_int("value"),
                shares=_int("shrsOrPrnAmt", "sshPrnamt"),
                share_type=_text(r, "shrsOrPrnAmt", "sshPrnamtType"),
            ))
    total = sum(h.value for h in holdings)
    aggregates = {
        "total_value": total,
        "num_positions": len(holdings),
        "top": sorted(holdings, key=lambda h: h.value, reverse=True)[:10],
    }
    return holdings, aggregates


# ---- rendering (HTML + text + normalized rows) ----
def render_form345_html(f: InsiderFiling) -> str:
    rows = "\n".join(
        f"<tr><td>{html.escape(t.date)}</td><td>{html.escape(t.security_title)}</td>"
        f"<td>{html.escape(t.code)}</td><td>{html.escape(t.acquired_disposed)}</td>"
        f"<td style='text-align:right'>{html.escape(t.shares)}</td>"
        f"<td style='text-align:right'>{html.escape(t.price)}</td>"
        f"<td style='text-align:right'>{html.escape(t.shares_after)}</td></tr>"
        for t in f.transactions
    )
    title = f"{f.owner_name} — Form {f.document_type} for {f.issuer_name}"
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head>"
        f"<body><h1>{html.escape(title)}</h1>"
        f"<p>Insider: {html.escape(f.owner_name)} (CIK {html.escape(f.owner_cik)})<br>"
        f"Role: {html.escape(f.role)}<br>"
        f"Issuer: {html.escape(f.issuer_name)} ({html.escape(f.issuer_symbol)}), CIK {html.escape(f.issuer_cik)}<br>"
        f"Period of report: {html.escape(f.period_of_report)}<br>"
        f"Data: SEC Form {html.escape(f.document_type)} (ownershipDocument)</p>"
        f"<table border='1' cellpadding='4' cellspacing='0'><thead><tr>"
        f"<th>Date</th><th>Security</th><th>Code</th><th>A/D</th><th>Shares</th>"
        f"<th>Price</th><th>Shares after</th></tr></thead><tbody>{rows}</tbody></table></body></html>"
    )


def form345_text(f: InsiderFiling) -> str:
    lines = [f"{f.owner_name} — Form {f.document_type} for {f.issuer_name} ({f.issuer_symbol})",
             f"Insider CIK {f.owner_cik}; role: {f.role}; period {f.period_of_report}", ""]
    for t in f.transactions:
        ad = {"A": "acquired", "D": "disposed"}.get(t.acquired_disposed, t.acquired_disposed)
        lines.append(f"{t.date}: {t.security_title} — code {t.code} ({ad}) "
                     f"{t.shares} sh @ {t.price}; held after: {t.shares_after}")
    return "\n".join(lines)


def form345_rows(issuer_cik: str, accession: str, f: InsiderFiling) -> list[dict]:
    return [{
        "cik": issuer_cik, "accession": accession, "doc_type": "E1",
        "owner_name": f.owner_name, "owner_cik": f.owner_cik, "role": f.role,
        "form": f.document_type, "table": t.table, "security_title": t.security_title,
        "transaction_date": t.date, "code": t.code, "acquired_disposed": t.acquired_disposed,
        "shares": t.shares, "price": t.price, "shares_after": t.shares_after,
    } for t in f.transactions]


def render_13f_html(holdings: list[Holding], agg: dict, *, filer: str, report: str) -> str:
    rows = "\n".join(
        f"<tr><td>{html.escape(h.issuer)}</td><td>{html.escape(h.cusip)}</td>"
        f"<td style='text-align:right'>{h.value:,}</td>"
        f"<td style='text-align:right'>{h.shares:,}</td></tr>"
        for h in agg["top"]
    )
    title = f"{filer} — 13F holdings"
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head>"
        f"<body><h1>{html.escape(title)}</h1>"
        f"<p>Filer: {html.escape(filer)}<br>Report: {html.escape(report)}<br>"
        f"Positions: {agg['num_positions']:,}<br>Total reported value: {agg['total_value']:,}<br>"
        f"Data: SEC 13F information table (value as reported — $thousands pre-2023, $ from 2023)</p>"
        f"<h2>Largest positions</h2>"
        f"<table border='1' cellpadding='4' cellspacing='0'><thead><tr>"
        f"<th>Issuer</th><th>CUSIP</th><th>Value</th><th>Shares</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></body></html>"
    )


def thirteenf_text(holdings: list[Holding], agg: dict, *, filer: str, report: str) -> str:
    lines = [f"{filer} — 13F holdings ({report})",
             f"{agg['num_positions']} positions; total reported value {agg['total_value']:,}",
             "Largest positions:"]
    for h in agg["top"]:
        lines.append(f"  {h.issuer} ({h.cusip}): value {h.value:,}, {h.shares:,} {h.share_type}")
    return "\n".join(lines)


def thirteenf_rows(filer_cik: str, accession: str, holdings: list[Holding], filing_date: date) -> list[dict]:
    unit = "USD" if filing_date >= date(2023, 1, 3) else "USD_thousands"
    return [{
        "cik": filer_cik, "accession": accession, "doc_type": "E2",
        "name_of_issuer": h.issuer, "cusip": h.cusip, "title_of_class": h.title_of_class,
        "value": h.value, "shares": h.shares, "share_type": h.share_type,
        "value_unit": unit,
    } for h in holdings]
