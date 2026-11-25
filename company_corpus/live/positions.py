"""Statement positions of one filing, rebuilt from its own EDGAR files (no FSDS bulk zip).

``filing_positions`` returns the rows ``opendata.statements`` builds from the SEC Financial
Statement Data Sets (``_read_quarter`` then the ``_extract`` dedup), so they flow through
``statement_rows`` and the concept selection unchanged. Three requests per filing: the folder
``index.json``, ``FilingSummary.xml`` (the rendered R reports) and the ``*_pre.xml`` presentation
linkbase (or the ``.xsd`` that embeds it).

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
* ``accepted`` is the UTC wall clock ``FilingRef`` carries, not FSDS's Eastern wall clock, in
  ``sub.txt``'s format with seconds zeroed (``2024-07-30 16:06:00.0``); inert, since
  ``statement_rows`` takes the acceptance time from its ``acceptance`` mapping;
* a filing without ``report_date`` gets ``period = ""``, and ``statement_rows`` drops its rows
  as ``statements_dropped:bad_period`` (the filing borrows positions instead);
* each locator of a presentation tree is expanded once, at its first place in the depth-first
  walk; a locator reached again from another parent is not repeated (bounds the work on
  diamond-shaped or cyclic arcs). A filer reusing one locator for a concept placed twice
  (opening/closing cash) loses the second place, which can shift later ``line`` numbers against
  FSDS: a known approximation, measured in A6;
* ``version`` is known only for the taxonomies of ``KNOWN_TAXONOMIES`` (by host and directory);
  any other schema, absolute or relative, is treated as an extension (the accession);
* prohibited arcs (``use="prohibited"``) are ignored rather than applied; a filing's own
  presentation linkbase seldom carries any;
* with several ``*_pre.xml``, the one FilingSummary lists in ``InputFiles`` is used (else the one
  sharing the stem of a listed ``.xsd``, else the first by name);
* a filing without ``*_pre.xml`` (filer agents such as DFIN embed every linkbase in the schema:
  Microsoft and Oracle since 2025) is read from its ``.xsd`` (the one ``InputFiles`` lists, else
  the first by name): the ``presentationLink`` elements of its appinfo are walked alike. A schema
  without any counts ``positions:no_presentation``;
* a period ending exactly between two month ends rounds to the later one.

Failure mode: a file that is missing (404 or absent from ``index.json``), malformed, or carries a
DOCTYPE gives no positions (``[]``) and one ``positions:<reason>`` count in ``counters``
(``no_index``, ``no_filing_summary``, ``no_statement_reports``, ``no_presentation``, ``malformed``,
``no_rows`` when no statement role is in the linkbase); it never raises. Network errors other than
404 (``SecUnavailable``, ``SecRefused``) propagate. XML is parsed by expat (the parser
``xml.etree.ElementTree`` uses) into an ``ElementTree``, with handlers that refuse a DOCTYPE or an
entity declaration as soon as expat reports it, whatever the document's encoding: no entity is ever
declared, so none is expanded and nothing external is loaded.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from xml.parsers import expat
from collections import Counter
from datetime import date, timedelta
from urllib.parse import urlsplit

from ..opendata.statements import KEPT_STATEMENTS
from .filings import FilingRef
from .sec import SecGet, SecNotFound

INDEX_FILE = "index.json"
SUMMARY_FILE = "FilingSummary.xml"
PRESENTATION_SUFFIX = "_pre.xml"
SCHEMA_SUFFIX = ".xsd"

#: Ordered ``(statement, pattern)`` rules over a lower-cased report name; first match wins. A
#: combined "income/operations/earnings ... and comprehensive income" statement is IS, an equity
#: statement that also shows comprehensive income is EQ, as FSDS files them.
STATEMENT_RULES: tuple[tuple[str, str], ...] = (
    ("CF", r"cash\s*flows?"),
    ("BS", r"balance\s*sheets?|financial\s*(?:position|condition)|assets\s*and\s*liabilities"
           r"|statements?\s*of\s*condition"),
    ("EQ", r"\bequity\b|\bdeficit\b|\bcapital\b|net\s*assets"),
    ("IS", r"\boperations?\b|\bearnings\b"),
    ("IS", r"\b(?:income|loss(?:es)?|profit)\b.*\bcomprehensive\b"),
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
#: Standard taxonomies -> FSDS ``version``: ``(hosts, pattern over the schema URL path, template)``,
#: first match wins. Any other schema (relative, or absolute on another host) is an extension.
_FASB_HOSTS = frozenset({"xbrl.fasb.org", "taxonomies.xbrl.us", "xbrl.us"})
KNOWN_TAXONOMIES: tuple[tuple[frozenset[str], str, str], ...] = (
    # 2009-2010 releases shipped dei inside the us-gaap tree: .../us-gaap/2009/non-gaap/dei-2009-01-31.xsd
    (_FASB_HOSTS, r"^/us-gaap/(?P<year>\d{4})/non-gaap/dei-", "dei/{year}"),
    (_FASB_HOSTS, r"^/(?P<ns>us-gaap|srt)/(?P<year>\d{4})/", "{ns}/{year}"),
    # XBRL US 2008 release: /us-gaap/1.0/elts/us-gaap-2008-03-31.xsd
    (_FASB_HOSTS, r"^/us-gaap/1\.0/.*-(?P<year>\d{4})-\d{2}-\d{2}\.xsd$", "us-gaap/{year}"),
    (frozenset({"xbrl.sec.gov"}), r"^/(?P<ns>[a-z][a-z-]*)/(?P<year>\d{4})/", "{ns}/{year}"),
    (frozenset({"xbrl.ifrs.org"}), r"^/taxonomy/(?P<year>\d{4})-\d{2}-\d{2}/", "ifrs/{year}"),
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


def _refuse(*_):
    raise _Malformed("DOCTYPE or entity declaration refused")


def _qname(name: str) -> str:
    return "{" + name if "}" in name else name


def _parse_xml(body: bytes) -> ET.Element:
    """An ElementTree built by expat, refusing any DOCTYPE or entity declaration (in any encoding
    expat detects) before anything in it is used."""
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    parser.UnparsedEntityDeclHandler = _refuse
    parser.ExternalEntityRefHandler = _refuse
    parser.StartElementHandler = lambda name, attrs: builder.start(
        _qname(name), {_qname(k): v for k, v in attrs.items()})
    parser.EndElementHandler = lambda name: builder.end(_qname(name))
    parser.CharacterDataHandler = builder.data
    parser.buffer_text = True
    try:
        parser.Parse(body, True)
        return builder.close()
    except (expat.ExpatError, LookupError, ValueError, AssertionError) as exc:   # ValueError: unsupported encoding
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


def _statement_reports(body: bytes) -> tuple[list[tuple[str, int, str]], list[str]]:
    """``(role, report number, statement)`` of the summary's kept, non-parenthetical statements,
    and the summary's ``InputFiles`` names."""
    root = _parse_xml(body)
    if _local(root.tag) != "FilingSummary":
        raise _Malformed("FilingSummary.xml: unexpected root")
    inputs = [(el.text or "").strip() for el in root.iter()
              if _local(el.tag) == "File" and (el.text or "").strip()]
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
    return out, inputs


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
        host = (parts.hostname or "").lower()
        for hosts, pattern, template in KNOWN_TAXONOMIES:
            m = re.match(pattern, parts.path) if host in hosts else None
            if m:
                return tag, template.format(**m.groupdict())
    return tag, accession


def _order(arc: ET.Element) -> float:
    try:
        return float(arc.get("order", "1"))
    except ValueError:
        return 1.0


def _walk(link: ET.Element) -> list[str]:
    """Locator hrefs of one presentation link in depth-first order (children by ``order``).

    Each locator label is emitted once, at its first place in the walk. A filer can reuse one
    locator for the same concept placed twice (opening and closing cash in a cash flow statement);
    its second place is then skipped, which can shift the ``line`` of everything after it against
    FSDS. Known approximation, to be measured in A6."""
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
    seen: set[str] = set()
    for root in labels:
        if root in targets:
            continue
        stack = [root]   # iterative preorder: no recursion limit on deep chains
        while stack:
            label = stack.pop()
            if label in seen or label not in hrefs:
                continue
            seen.add(label)
            out.append(hrefs[label])
            stack.extend(child for _, _, child in sorted(children.get(label, ()), reverse=True))
    return out


def _presentation_hrefs(body: bytes, roles: set[str]) -> tuple[dict[str, list[str]], bool]:
    """Hrefs of the statement roles' presentation links, and whether the document has any
    presentation link. Reads a linkbase, or a schema embedding its linkbases in its appinfo."""
    root = _parse_xml(body)
    if _local(root.tag) not in ("linkbase", "schema"):
        raise _Malformed("presentation linkbase: unexpected root")
    out: dict[str, list[str]] = {}
    found = False
    for link in root.iter(f"{_LINK}presentationLink"):
        found = True
        role = link.get(f"{_XLINK}role")
        if role in roles:
            out.setdefault(role, []).extend(_walk(link))
    return out, found


def _month_end(d: date) -> date:
    first_of_next = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return first_of_next - timedelta(days=1)


def fsds_period(d: date | None) -> str:
    """The report date rounded to the nearest month end, as ``YYYYMMDD`` ('' when unknown)."""
    if d is None:
        return ""
    later, earlier = _month_end(d), d.replace(day=1) - timedelta(days=1)
    return (earlier if (d - earlier) < (later - d) else later).strftime("%Y%m%d")


def _presentation_name(names: list[str], inputs: list[str]) -> str | None:
    """The filing's presentation linkbase: the one FilingSummary rendered from, else the one
    sharing the stem of a rendered schema, else the first by name."""
    candidates = sorted(n for n in names if n.endswith(PRESENTATION_SUFFIX))
    for n in inputs:
        if n in candidates:
            return n
    for n in inputs:
        if n.endswith(".xsd") and f"{n[:-4]}{PRESENTATION_SUFFIX}" in candidates:
            return f"{n[:-4]}{PRESENTATION_SUFFIX}"
    return candidates[0] if candidates else None


def _schema_name(names: list[str], inputs: list[str]) -> str | None:
    """The filing's schema, read when no ``*_pre.xml`` exists (embedded linkbases): the one
    FilingSummary rendered from, else the first by name."""
    candidates = sorted(n for n in names if n.endswith(SCHEMA_SUFFIX))
    for n in inputs:
        if n in candidates:
            return n
    return candidates[0] if candidates else None


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
        reports, inputs = _statement_reports(summary)
        if not reports:
            counters["positions:no_statement_reports"] += 1
            return []
        pre_name = _presentation_name(names, inputs) or _schema_name(names, inputs)
        pre = _fetch(sec, f"{base}/{pre_name}") if pre_name else None
        if pre is None:
            counters["positions:no_presentation"] += 1
            return []
        hrefs, has_links = _presentation_hrefs(pre, {role for role, _, _ in reports})
        if not has_links:
            counters["positions:no_presentation"] += 1
            return []
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
    if not kept:
        counters["positions:no_rows"] += 1
    return [kept[k] for k in sorted(kept)]
