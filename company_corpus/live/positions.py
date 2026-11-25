"""Statement positions of one filing, rebuilt from its own EDGAR files (no FSDS bulk zip).

``filing_positions`` returns the rows ``opendata.statements`` builds from the SEC Financial
Statement Data Sets (``_read_quarter`` then the ``_extract`` dedup), so they flow through
``statement_rows`` and the concept selection unchanged. Three requests per filing: the folder
``index.json``, ``FilingSummary.xml`` (the rendered R reports) and the ``*_pre.xml`` presentation
linkbase.

Reproduced from FSDS:

* row shape ``{accn, form, period, filed, accepted, statement, tag, version, plabel, report, line}``;
  ``period``/``filed`` as ``YYYYMMDD``; ``report``/``line`` ints;
* ``report`` is the R-file number of the rendered report (``R4.htm`` -> 4);
* ``line`` is the 1-based position in the depth-first walk of the report's presentation tree
  (children by numeric ``order``), every locator counted, abstracts included;
* ``version`` is ``<prefix>/<year>`` for a standard taxonomy (``us-gaap/2024``, ``srt/2024``,
  ``dei/2024``, ``ifrs/2024``), read from the locator's schema URL, and the accession for an
  extension element;
* ``period`` is the report date rounded to the nearest month end (DERA's "balance sheet date,
  rounded to nearest month-end");
* only ``KEPT_STATEMENTS``, parenthetical reports dropped, one row per ``(accn, statement, tag)``
  keeping the lowest ``(report, line)``, rows ordered by that key.

Approximations (A6 measures them against FSDS on real issuers):

* ``stmt`` is DERA's own classification; here it comes from the report names by the ordered
  rules in ``STATEMENT_RULES`` (data, first match wins), and only reports in ``MenuCategory``
  ``Statements`` (or, in old summaries without a menu, whose long name says ``Statement``) count;
* ``plabel`` is the empty string: the label linkbase is not fetched (a fourth, often 1 MB request)
  and nothing downstream reads ``plabel`` beyond storing it;
* ``accepted`` is the acceptance wall clock as ``FilingRef`` carries it, minutes kept and seconds
  zeroed (``2024-07-30 16:06:00.0``), the way ``sub.txt`` prints it; ``statement_rows`` takes the
  acceptance time from its ``acceptance`` mapping, not from this field;
* prohibited arcs (``use="prohibited"``) are ignored rather than applied; a filing's own
  presentation linkbase seldom carries any;
* a period ending exactly between two month ends rounds to the later one.

Failure mode: a file that is missing (404 or absent from ``index.json``), malformed, or carries a
DOCTYPE gives no positions (``[]``) and one ``positions:<reason>`` count in ``counters``; it never
raises. Network errors other than 404 (``SecUnavailable``, ``SecRefused``) propagate. XML is parsed
with ``xml.etree.ElementTree`` after refusing any DOCTYPE, so no entity is ever expanded.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, timedelta
from urllib.parse import urlsplit

from ..opendata.statements import KEPT_STATEMENTS
from .filings import FilingRef
from .sec import SecGet, SecNotFound

INDEX_FILE = "index.json"
SUMMARY_FILE = "FilingSummary.xml"
PRESENTATION_SUFFIX = "_pre.xml"

#: Ordered ``(statement, pattern)`` rules over a lower-cased report name; first match wins. A
#: combined "operations and comprehensive income" statement is IS, an equity statement that also
#: shows comprehensive income is EQ, as FSDS files them.
STATEMENT_RULES: tuple[tuple[str, str], ...] = (
    ("CF", r"cash\s*flows?"),
    ("BS", r"balance\s*sheets?|financial\s*(?:position|condition)|assets\s*and\s*liabilities"
           r"|statements?\s*of\s*condition"),
    ("EQ", r"\bequity\b|\bdeficit\b|\bcapital\b|net\s*assets"),
    ("IS", r"\boperations?\b|\bearnings\b"),
    ("CI", r"comprehensive"),
    ("IS", r"\bincome\b|\bloss(?:es)?\b|\bprofit\b"),
)
#: A report name matching one of these is never a statement (detail, table and policy reports).
NOT_STATEMENT = (r"\((?:details?|tables?|polic(?:y|ies))\)", r"\s-\s(?:disclosure|document)\s-\s")
PARENTHETICAL = r"parenthetical"
#: Reports taken from FilingSummary: this menu category, or, when a summary has no menu (old
#: filings), a long name like ``"100030 - Statement - BALANCE SHEETS"``.
STATEMENT_MENU = "Statements"
STATEMENT_LONG_NAME = r"^\s*\d+\s*-\s*statement\s*-"
#: Standard taxonomy schema file names -> FSDS ``version``; anything else is an extension.
STANDARD_SCHEMAS: tuple[tuple[str, str], ...] = (
    (r"^(?P<prefix>[a-z][a-z0-9-]*?)-(?P<year>\d{4})(?:-\d{2}-\d{2})?\.xsd$", "{prefix}/{year}"),
    (r"^full_ifrs-cor_(?P<year>\d{4})-\d{2}-\d{2}\.xsd$", "ifrs/{year}"),
)

_LINK = "{http://www.xbrl.org/2003/linkbase}"
_XLINK = "{http://www.w3.org/1999/xlink}"
_R_FILE = re.compile(r"^R(\d+)\.(?:htm|xml)$", re.IGNORECASE)


class _Malformed(ValueError):
    pass


def _role_words(role: str) -> str:
    last = role.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", last)


def classify_report(short_name: str, long_name: str, role: str) -> tuple[str | None, bool]:
    """``(statement, parenthetical)`` of a rendered report: the short name decides, then the long
    name, then the role URI's last segment; ``None`` when no rule matches or a name is a detail."""
    texts = [t.lower().replace("’", "'") for t in (short_name or "", long_name or "",
                                                        _role_words(role or "")) if t]
    if any(re.search(p, t) for p in NOT_STATEMENT for t in texts):
        return None, False
    parenthetical = any(re.search(PARENTHETICAL, t) for t in texts)
    for text in texts:
        for statement, pattern in STATEMENT_RULES:
            if re.search(pattern, text):
                return statement, parenthetical
    return None, False


def _parse_xml(body: bytes) -> ET.Element:
    try:   # a UTF-16 document would hide its DOCTYPE from a byte search
        head = body.decode("utf-16") if body[:2] in (b"\xff\xfe", b"\xfe\xff") else body.decode("latin-1")
    except UnicodeDecodeError as exc:
        raise _Malformed(str(exc)) from exc
    if "<!DOCTYPE" in head.upper():
        raise _Malformed("DOCTYPE refused")
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise _Malformed(str(exc)) from exc


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(el: ET.Element, name: str) -> str:
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _index_names(body: bytes) -> list[str]:
    try:
        items = json.loads(body)["directory"]["item"]
        return [i["name"] for i in items if isinstance(i.get("name"), str)]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _Malformed(f"index.json: {exc}") from exc


def _statement_reports(body: bytes) -> list[tuple[str, int, str]]:
    """``(role, report number, statement)`` of the summary's kept, non-parenthetical statements."""
    root = _parse_xml(body)
    if _local(root.tag) != "FilingSummary":
        raise _Malformed("FilingSummary.xml: unexpected root")
    out = []
    for report in root.iter():
        if _local(report.tag) != "Report":
            continue
        menu, long_name = _child_text(report, "MenuCategory"), _child_text(report, "LongName")
        if menu != STATEMENT_MENU and (menu or not re.search(STATEMENT_LONG_NAME, long_name, re.I)):
            continue
        role = _child_text(report, "Role")
        statement, parenthetical = classify_report(_child_text(report, "ShortName"), long_name, role)
        if statement not in KEPT_STATEMENTS or parenthetical or not role:
            continue
        number = _report_number(report)
        if number is not None:
            out.append((role, number, statement))
    return out


def _report_number(report: ET.Element) -> int | None:
    m = _R_FILE.match(_child_text(report, "HtmlFileName"))
    if m:
        return int(m.group(1))
    position = _child_text(report, "Position")
    return int(position) if position.isdigit() else None


def _tag_version(href: str, accession: str) -> tuple[str, str] | None:
    """``(tag, version)`` of a locator href; None when the href names no element."""
    schema, _, fragment = href.partition("#")
    if not fragment:
        return None
    tag = fragment.split("_", 1)[1] if "_" in fragment else fragment
    parts = urlsplit(schema)
    if parts.scheme in ("http", "https"):
        name = parts.path.rsplit("/", 1)[-1]
        for pattern, template in STANDARD_SCHEMAS:
            m = re.match(pattern, name)
            if m:
                return tag, template.format(**m.groupdict())
    return tag, accession


def _order(arc: ET.Element) -> float:
    try:
        return float(arc.get("order", "1"))
    except ValueError:
        return 1.0


def _walk(link: ET.Element) -> list[str]:
    """Locator hrefs of one presentation link in depth-first order (children by ``order``)."""
    hrefs: dict[str, str] = {}
    labels: list[str] = []
    children: dict[str, list[tuple[float, int, str]]] = {}
    targets: set[str] = set()
    for i, el in enumerate(link):
        name = _local(el.tag)
        if name == "loc":
            label, href = el.get(f"{_XLINK}label"), el.get(f"{_XLINK}href")
            if label and href and label not in hrefs:
                hrefs[label] = href
                labels.append(label)
        elif name == "presentationArc" and el.get("use") != "prohibited":
            src, dst = el.get(f"{_XLINK}from"), el.get(f"{_XLINK}to")
            if src and dst:
                children.setdefault(src, []).append((_order(el), i, dst))
                targets.add(dst)
    out: list[str] = []

    def visit(label: str, path: frozenset[str]) -> None:
        if label in path or label not in hrefs:
            return
        out.append(hrefs[label])
        for _, _, child in sorted(children.get(label, ())):
            visit(child, path | {label})

    for label in labels:
        if label not in targets:
            visit(label, frozenset())
    return out


def _presentation_hrefs(body: bytes, roles: set[str]) -> dict[str, list[str]]:
    root = _parse_xml(body)
    if _local(root.tag) != "linkbase":
        raise _Malformed("presentation linkbase: unexpected root")
    out: dict[str, list[str]] = {}
    for link in root.iter(f"{_LINK}presentationLink"):
        role = link.get(f"{_XLINK}role")
        if role in roles:
            out.setdefault(role, []).extend(_walk(link))
    return out


def _month_end(d: date) -> date:
    first_of_next = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return first_of_next - timedelta(days=1)


def fsds_period(d: date | None) -> str:
    """The report date rounded to the nearest month end, as ``YYYYMMDD`` ('' when unknown)."""
    if d is None:
        return ""
    later, earlier = _month_end(d), d.replace(day=1) - timedelta(days=1)
    return (earlier if (d - earlier) < (later - d) else later).strftime("%Y%m%d")


def _fetch(sec: SecGet, url: str) -> bytes | None:
    try:
        return sec.get_bytes(url, max_age_s=None)
    except SecNotFound:
        return None


def filing_positions(ref: FilingRef, sec: SecGet, *, counters: Counter | None = None) -> list[dict]:
    """FSDS-shaped statement rows of one filing (see the module docstring); ``[]`` with one
    ``positions:<reason>`` count when its files are missing or unreadable."""
    counters = counters if counters is not None else Counter()
    base = ref.url.rstrip("/")
    try:
        index = _fetch(sec, f"{base}/{INDEX_FILE}")
        if index is None:
            counters["positions:no_index"] += 1
            return []
        names = _index_names(index)
        summary = _fetch(sec, f"{base}/{SUMMARY_FILE}") if SUMMARY_FILE in names else None
        if summary is None:
            counters["positions:no_filing_summary"] += 1
            return []
        reports = _statement_reports(summary)
        if not reports:
            counters["positions:no_statement_reports"] += 1
            return []
        pre_names = sorted(n for n in names if n.endswith(PRESENTATION_SUFFIX))
        pre = _fetch(sec, f"{base}/{pre_names[0]}") if pre_names else None
        if pre is None:
            counters["positions:no_presentation"] += 1
            return []
        hrefs = _presentation_hrefs(pre, {role for role, _, _ in reports})
    except _Malformed:
        counters["positions:malformed"] += 1
        return []
    submission = {"accn": ref.accession, "form": ref.form, "period": fsds_period(ref.report_date),
                  "filed": ref.filed.strftime("%Y%m%d") if ref.filed else "",
                  "accepted": ref.accepted.strftime("%Y-%m-%d %H:%M:00.0") if ref.accepted else ""}
    kept: dict[tuple[str, str, str], dict] = {}
    for role, number, statement in reports:
        for line, href in enumerate(hrefs.get(role, ()), 1):
            parsed = _tag_version(href, ref.accession)
            if parsed is None:
                continue
            tag, version = parsed
            key = (ref.accession, statement, tag)
            if key not in kept or (number, line) < (kept[key]["report"], kept[key]["line"]):
                kept[key] = {**submission, "statement": statement, "tag": tag, "version": version,
                             "plabel": "", "report": number, "line": line}
    return [kept[k] for k in sorted(kept)]
