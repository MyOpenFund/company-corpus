import json
import re
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from company_corpus.config import Config
from company_corpus.live.filings import FilingRef
from company_corpus.live.positions import (
    MAX_SCHEMAS, _tag_version, _walk, classify_report, filing_positions,
)
from company_corpus.live.sec import SecNotFound
from company_corpus.opendata.statements import KEPT_STATEMENTS, statement_rows

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "live"
CIK, ACCN = "0000789019", "0000950170-24-087843"
FOLDER = "https://www.sec.gov/Archives/edgar/data/789019/000095017024087843"
FILING_DIR = FIXTURES / CIK / ACCN
PRE = "msft-20240630_pre.xml"
REF = FilingRef(
    cik=CIK, accession=ACCN, form="10-K", report_date=date(2024, 6, 30), filed=date(2024, 7, 30),
    accepted=datetime(2024, 7, 30, 16, 6, 22, tzinfo=timezone.utc), primary_document="msft-10k_20240630.htm",
    url=FOLDER, doc_id=None)


class FakeSec:
    """Serves ``{url: bytes}``; any other URL is a 404, like EDGAR."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_bytes(self, url, *, max_age_s=None):
        self.calls.append(url)
        if url not in self.routes:
            raise SecNotFound(url)
        return self.routes[url]


def _routes(**override):
    files = {name: (FILING_DIR / name).read_bytes() for name in ("index.json", "FilingSummary.xml", PRE)}
    files.update(override)
    return {f"{FOLDER}/{name}": body for name, body in files.items() if body is not None}


def _expected():
    path = FIXTURES / "fsds" / f"{ACCN}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.mark.parametrize("short_name, expected", [
    ("Consolidated Balance Sheets", ("BS", False)),
    ("CONSOLIDATED STATEMENTS OF OPERATIONS", ("IS", False)),
    ("Income Statements", ("IS", False)),
    ("Cash Flows Statements", ("CF", False)),
    ("Consolidated Balance Sheets (Parenthetical)", ("BS", True)),
    ("Comprehensive Income Statements", ("CI", False)),
    ("Stockholders' Equity Statements", ("EQ", False)),
    ("Revenue - Additional Information (Details)", (None, False)),
    ("Consolidated Statements of Financial Condition", ("BS", False)),
    ("Consolidated Statements of Operations and Comprehensive Income", ("IS", False)),
    ("Consolidated Statements of Changes in Shareholders' Equity and Comprehensive Income", ("EQ", False)),
    ("Consolidated Statements of Earnings", ("IS", False)),
    ("Consolidated Statements of Income and Comprehensive Income", ("IS", False)),
    ("Consolidated Statements of Operations and Comprehensive Loss", ("IS", False)),
    ("Consolidated Statements of Comprehensive Income (Loss)", ("CI", False)),
    ("Income Taxes (Details)", (None, False)),
    # IFRS filers (Novo Nordisk 20-F) title a sub-report "<group> - <statement>": the statement
    # after the last " - " decides, as FSDS classifies it (CI here, not the group's IS).
    ("Income statement and Statement of comprehensive income", ("IS", False)),
    ("Income statement and Statement of comprehensive income - Statement of comprehensive income",
     ("CI", False)),
    ("Consolidated Statements of Cash Flows - Supplemental", ("CF", False)),
])
def test_classify_report_cases(short_name, expected):
    assert classify_report(short_name, "", "") == expected


@pytest.mark.parametrize("short_name, expected", [
    # The last " - " segment decides only when it reads as a statement title itself; a line or
    # topic name after an unambiguous statement title keeps the head's classification.
    ("CONSOLIDATED STATEMENTS OF CASH FLOWS - Capital expenditures", ("CF", False)),
    ("Statement of Financial Position - Loss allowance", ("BS", False)),
    ("Balance Sheet - Equity", ("BS", False)),
    ("Cash flow statement - Profit", ("CF", False)),
    ("Statement of changes in equity - Retained earnings", ("EQ", False)),
    ("Consolidated Statements of Comprehensive Income - Net income", ("CI", False)),
    ("Consolidated Statements of Stockholders' Equity - Comprehensive Income", ("EQ", False)),
    ("Balance Sheets - X Income Fund", ("BS", False)),
    ("Consolidated Balance Sheet - Parent Company - Equity", ("BS", False)),
    ("Statement - Consolidated Balance Sheets", ("BS", False)),
    # Only a spaced hyphen separates: an en dash or an unspaced hyphen reads the whole name.
    ("Income statement and Statement of comprehensive income \u2013 Statement of comprehensive income",
     ("IS", False)),
    ("Income statement and Statement of comprehensive income-Statement of comprehensive income",
     ("IS", False)),
])
def test_classify_report_last_segment_decides_only_as_a_statement_title(short_name, expected):
    assert classify_report(short_name, "", "") == expected


def test_classify_report_long_name_with_a_bare_statement_segment_falls_back():
    assert classify_report("", "100040 - Statement - Statement",
                           "http://x/role/ConsolidatedStatementsOfIncome") == ("IS", False)
    assert classify_report("", "0000004 - Statement - CONSOLIDATED BALANCE SHEETS", "") == ("BS", False)


def test_classify_report_falls_back_on_long_name_then_role():
    assert classify_report("", "100030 - Statement - BALANCE SHEETS", "") == ("BS", False)
    assert classify_report("Statement 3", "", "http://www.example.com/role/CashFlowsStatements") == ("CF", False)
    assert classify_report("", "", "http://www.example.com/role/BalanceSheetsParenthetical") == ("BS", True)


def test_positions_match_hand_written_fsds_rows():
    rows = filing_positions(REF, FakeSec(_routes()))
    expected = _expected()
    assert {(r["statement"], r["tag"]) for r in rows} == {(r["statement"], r["tag"]) for r in expected}
    # Every FSDS field but the label text (plabel: the label linkbase is not fetched, see the module).
    strip = lambda r: {k: v for k, v in r.items() if k != "plabel"}
    assert sorted(map(strip, rows), key=lambda r: (r["statement"], r["tag"])) \
        == sorted(map(strip, expected), key=lambda r: (r["statement"], r["tag"]))
    assert all(set(r) == set(expected[0]) for r in rows)
    assert all(isinstance(r["report"], int) and isinstance(r["line"], int) for r in rows)


def test_rows_are_kept_statements_without_parenthetical_or_details():
    rows = filing_positions(REF, FakeSec(_routes()))
    assert {r["statement"] for r in rows} == set(KEPT_STATEMENTS)
    tags = {r["tag"] for r in rows}
    assert "CommonStockSharesAuthorized" not in tags        # parenthetical only
    assert "RevenueRemainingPerformanceObligation" not in tags   # Details report
    assert "DocumentType" not in tags                        # Cover report
    assert "OtherComprehensiveIncomeLossNetOfTax" not in tags  # CI statement
    assert "DividendsCommonStockCash" not in tags            # EQ statement


def test_dedup_keeps_lowest_line_and_tag_on_two_statements():
    rows = filing_positions(REF, FakeSec(_routes()))
    cash = [r for r in rows if r["tag"] == "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
    assert [(r["statement"], r["report"], r["line"]) for r in cash] == [("CF", 6, 21)]
    net = sorted((r["statement"], r["report"], r["line"]) for r in rows if r["tag"] == "NetIncomeLoss")
    assert net == [("CF", 6, 3), ("IS", 2, 18)]
    assert len({(r["accn"], r["statement"], r["tag"]) for r in rows}) == len(rows)


def test_rows_are_sorted_like_extract():
    rows = filing_positions(REF, FakeSec(_routes()))
    assert [(r["accn"], r["statement"], r["tag"]) for r in rows] \
        == sorted((r["accn"], r["statement"], r["tag"]) for r in rows)


def test_version_standard_and_custom():
    rows = {r["tag"]: r for r in filing_positions(REF, FakeSec(_routes()))}
    assert rows["RevenueFromContractWithCustomerExcludingAssessedTax"]["version"].startswith("us-gaap/")
    assert rows["RevenueFromContractWithCustomerExcludingAssessedTax"]["version"] == "us-gaap/2024"
    assert rows["ProductOrServiceAxis"]["version"] == "srt/2024"
    assert rows["RecognitionOfUnearnedRevenue"]["version"] == ACCN
    assert rows["ShortTermIncomeTaxes"]["version"] == ACCN


def test_submission_fields_in_fsds_formats():
    (r, *_) = filing_positions(REF, FakeSec(_routes()))
    assert (r["accn"], r["form"], r["period"], r["filed"], r["accepted"]) \
        == (ACCN, "10-K", "20240630", "20240730", "2024-07-30 16:06:00.0")


@pytest.mark.parametrize("report_date, period", [
    (date(2024, 6, 30), "20240630"), (date(2024, 6, 29), "20240630"), (date(2024, 7, 2), "20240630"),
    (date(2023, 12, 30), "20231231"), (date(2024, 1, 3), "20231231"), (date(2024, 2, 20), "20240229"),
])
def test_period_is_the_report_date_rounded_to_nearest_month_end(report_date, period):
    (r, *_) = filing_positions(replace(REF, report_date=report_date), FakeSec(_routes()))
    assert r["period"] == period


def test_missing_dates_give_empty_strings():
    (r, *_) = filing_positions(replace(REF, report_date=None, filed=None, accepted=None), FakeSec(_routes()))
    assert (r["period"], r["filed"], r["accepted"]) == ("", "", "")


def test_requests_are_minimal():
    sec = FakeSec(_routes())
    filing_positions(REF, sec)
    assert sec.calls == [f"{FOLDER}/index.json", f"{FOLDER}/FilingSummary.xml", f"{FOLDER}/{PRE}"]


def test_missing_filing_summary_returns_empty():
    counters = Counter()
    assert filing_positions(REF, FakeSec(_routes(**{"FilingSummary.xml": None})), counters=counters) == []
    assert counters["positions:no_filing_summary"] == 1


def test_filing_summary_absent_from_index_is_not_requested():
    index = json.loads((FILING_DIR / "index.json").read_text())
    index["directory"]["item"] = [i for i in index["directory"]["item"] if i["name"] != "FilingSummary.xml"]
    sec, counters = FakeSec(_routes(**{"index.json": json.dumps(index).encode()})), Counter()
    assert filing_positions(REF, sec, counters=counters) == []
    assert sec.calls == [f"{FOLDER}/index.json"] and counters["positions:no_filing_summary"] == 1


def test_missing_presentation_linkbase_returns_empty():
    index = json.loads((FILING_DIR / "index.json").read_text())
    index["directory"]["item"] = [i for i in index["directory"]["item"] if not i["name"].endswith("_pre.xml")]
    counters = Counter()
    assert filing_positions(REF, FakeSec(_routes(**{"index.json": json.dumps(index).encode()})),
                            counters=counters) == []
    assert counters["positions:no_presentation"] == 1


SCHEMA = "msft-20240630.xsd"


def _schema_embedding(linkbase: str) -> bytes:
    """A filing schema carrying ``linkbase`` in its appinfo, as some filer agents ship it (no
    separate ``*_pre.xml``)."""
    body = re.sub(r"^<\?xml[^>]*\?>\s*", "", linkbase)
    return ('<?xml version="1.0" encoding="US-ASCII"?>\n'
            '<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:link="http://www.xbrl.org/2003/linkbase" targetNamespace="http://example.com/x">'
            f'<xsd:annotation><xsd:appinfo>{body}</xsd:appinfo></xsd:annotation>'
            '<xsd:element name="Widgets" id="abc_Widgets"/></xsd:schema>').encode("ascii")


def _index_without_pre() -> bytes:
    index = json.loads((FILING_DIR / "index.json").read_text())
    index["directory"]["item"] = [i for i in index["directory"]["item"] if not i["name"].endswith("_pre.xml")]
    return json.dumps(index).encode()


def test_presentation_embedded_in_the_schema_is_read():
    """Filer agents such as DFIN embed every linkbase in the ``.xsd`` (Microsoft and Oracle filings
    since 2025): the rows are the ones the separate linkbase gives."""
    summary = (FILING_DIR / "FilingSummary.xml").read_text().replace(f"<File>{PRE}</File>", "")
    sec = FakeSec(_routes(**{"index.json": _index_without_pre(), PRE: None,
                             "FilingSummary.xml": summary.encode(),
                             SCHEMA: _schema_embedding((FILING_DIR / PRE).read_text())}))
    counters = Counter()
    assert filing_positions(REF, sec, counters=counters) == filing_positions(REF, FakeSec(_routes()))
    assert sec.calls == [f"{FOLDER}/index.json", f"{FOLDER}/FilingSummary.xml", f"{FOLDER}/{SCHEMA}"]
    assert not counters


def test_schema_without_presentation_counts_no_presentation():
    linkbase = ('<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase">'
                '<link:roleRef roleURI="http://x/role"/></link:linkbase>')
    counters = Counter()
    sec = FakeSec(_routes(**{"index.json": _index_without_pre(), PRE: None,
                             SCHEMA: _schema_embedding(linkbase)}))
    assert filing_positions(REF, sec, counters=counters) == []
    assert counters == Counter({"positions:no_presentation": 1})


def test_missing_index_returns_empty():
    counters = Counter()
    assert filing_positions(REF, FakeSec({}), counters=counters) == []
    assert counters["positions:no_index"] == 1


@pytest.mark.parametrize("name, body", [
    ("index.json", b"{not json"),
    ("index.json", b'{"directory": []}'),
    ("FilingSummary.xml", b"<FilingSummary><MyReports>"),
    (PRE, b"<link:linkbase xmlns:link='x'><unclosed>"),
    ("FilingSummary.xml", b'<?xml version="1.0" encoding="shift_jis"?><FilingSummary/>'),
    ("FilingSummary.xml", b'<?xml version="1.0" encoding="utf-32"?><FilingSummary/>'),
    (PRE, b'<?xml version="1.0" encoding="gbk"?><link:linkbase xmlns:link="x"/>'),
])
def test_malformed_files_give_no_positions_and_a_counter(name, body):
    counters = Counter()
    assert filing_positions(REF, FakeSec(_routes(**{name: body})), counters=counters) == []
    assert counters["positions:malformed"] == 1


def test_doctype_is_refused_before_parsing():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
            b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><FilingSummary>&b;</FilingSummary>')
    external = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
    declared = bomb.decode().replace('version="1.0"', 'version="1.0" encoding="utf-16"')
    undeclared = bomb.decode().replace('<?xml version="1.0"?>', "")
    utf16 = [declared.encode("utf-16"),                                  # with BOM
             declared.encode("utf-16-le"), declared.encode("utf-16-be"),  # BOM-less, declared
             undeclared.encode("utf-16-le"), undeclared.encode("utf-16-be")]  # BOM-less, no declaration
    # Were the entities expanded, the summary would parse and count no_statement_reports instead.
    for body in (bomb, external, *utf16, b"\xff\xfe\x00", b'<!DOCTYPE FilingSummary><FilingSummary/>'):
        counters = Counter()
        assert filing_positions(REF, FakeSec(_routes(**{"FilingSummary.xml": body})), counters=counters) == []
        assert counters == Counter({"positions:malformed": 1}), body[:40]


def test_no_statement_reports_returns_empty():
    summary = (FILING_DIR / "FilingSummary.xml").read_text().replace(
        "<MenuCategory>Statements</MenuCategory>", "<MenuCategory>Notes</MenuCategory>")
    counters = Counter()
    assert filing_positions(REF, FakeSec(_routes(**{"FilingSummary.xml": summary.encode()})),
                            counters=counters) == []
    assert counters["positions:no_statement_reports"] == 1


def test_report_without_menu_category_uses_long_name():
    summary = (FILING_DIR / "FilingSummary.xml").read_text().replace(
        "<MenuCategory>Statements</MenuCategory>", "")
    rows = filing_positions(REF, FakeSec(_routes(**{"FilingSummary.xml": summary.encode()})))
    assert {(r["statement"], r["tag"]) for r in rows} == {(r["statement"], r["tag"]) for r in _expected()}


def test_statement_rows_accepts_the_positions():
    rows = filing_positions(REF, FakeSec(_routes()))
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    res = statement_rows(CIK, rows, acceptance={ACCN: REF.accepted}, manifest_doc_ids=set(),
                         config=Config(contact="test@example.com"), now=now)
    assert len(res.rows) == len(rows) and res.counters["statements_dropped:bad_period"] == 0
    r = res.rows[0]
    assert r["filing_ref"] == ACCN and r["valid_date"] == date(2024, 6, 30) and r["known_at"] == REF.accepted


def _link(locs, arcs):
    import xml.etree.ElementTree as ET
    x = "http://www.w3.org/1999/xlink"
    link = ET.Element("{http://www.xbrl.org/2003/linkbase}presentationLink")
    for label in locs:
        ET.SubElement(link, "{http://www.xbrl.org/2003/linkbase}loc",
                      {f"{{{x}}}label": label, f"{{{x}}}href": f"s.xsd#p_{label}"})
    for src, dst, order in arcs:
        ET.SubElement(link, "{http://www.xbrl.org/2003/linkbase}presentationArc",
                      {f"{{{x}}}from": src, f"{{{x}}}to": dst, "order": str(order)})
    return link


def test_walk_handles_a_deep_chain_without_recursion():
    depth = 5000
    labels = [f"n{i}" for i in range(depth)]
    hrefs = _walk(_link(labels, [(labels[i], labels[i + 1], 1) for i in range(depth - 1)]))
    assert hrefs == [f"s.xsd#p_{label}" for label in labels]


def test_walk_expands_a_shared_locator_once_in_preorder():
    # A diamond stacked 40 times: a path-only guard would visit 2**40 nodes.
    layers = 40
    locs, arcs = ["root"], []
    prev = "root"
    for i in range(layers):
        a, b, join = f"a{i}", f"b{i}", f"j{i}"
        locs += [a, b, join]
        arcs += [(prev, a, 1), (prev, b, 2), (a, join, 1), (b, join, 1)]
        prev = join
    hrefs = _walk(_link(locs, arcs))
    assert len(hrefs) == len(locs) == len(set(hrefs))
    assert [h.split("_", 1)[1] for h in hrefs[:5]] == ["root", "a0", "j0", "a1", "j1"]
    cyclic = _walk(_link(["r", "x", "y"], [("r", "x", 1), ("x", "y", 1), ("y", "x", 1)]))
    assert cyclic == ["s.xsd#p_r", "s.xsd#p_x", "s.xsd#p_y"]


def test_deep_presentation_chain_does_not_crash_the_filing():
    pre = (FILING_DIR / PRE).read_text()
    x = 'xlink:type="locator"'
    deep = "".join(f'<link:loc {x} xlink:href="msft-20240630.xsd#msft_D{i}" xlink:label="d{i}"/>'
                   for i in range(3000))
    deep += "".join(f'<link:presentationArc xlink:type="arc" xlink:from="{"loc_us-gaap_StatementOfFinancialPositionAbstract" if i == 0 else f"d{i - 1}"}" xlink:to="d{i}" order="99"/>'
                    for i in range(3000))
    bs_open = '<link:presentationLink xlink:type="extended" xlink:role="http://www.microsoft.com/role/BalanceSheets">'
    assert pre.count(bs_open) == 1
    pre = pre.replace(bs_open, bs_open + deep)
    rows = filing_positions(REF, FakeSec(_routes(**{PRE: pre.encode()})))
    chain = sorted(r["line"] for r in rows if re.fullmatch(r"D\d+", r["tag"]))
    assert len(chain) == 3000 and chain == list(range(chain[0], chain[0] + 3000))


@pytest.mark.parametrize("href, version", [
    ("https://xbrl.fasb.org/us-gaap/2024/elts/us-gaap-2024.xsd#us-gaap_Revenues", "us-gaap/2024"),
    ("http://xbrl.fasb.org/us-gaap/2009/elts/us-gaap-2009-01-31.xsd#us-gaap_Revenues", "us-gaap/2009"),
    ("http://taxonomies.xbrl.us/us-gaap/2009/elts/us-gaap-std-2009-01-31.xsd#us-gaap_Revenues", "us-gaap/2009"),
    ("http://xbrl.us/us-gaap/1.0/elts/us-gaap-2008-03-31.xsd#us-gaap_Revenues", "us-gaap/2008"),
    ("http://taxonomies.xbrl.us/us-gaap/2009/non-gaap/dei-2009-01-31.xsd#dei_DocumentType", "dei/2009"),
    ("https://xbrl.fasb.org/srt/2024/elts/srt-2024.xsd#srt_ProductOrServiceAxis", "srt/2024"),
    ("https://xbrl.sec.gov/dei/2024/dei-2024.xsd#dei_DocumentType", "dei/2024"),
    ("https://xbrl.ifrs.org/taxonomy/2024-03-27/full_ifrs/full_ifrs-cor_2024-03-27.xsd#ifrs-full_Revenue",
     "ifrs/2024"),
    ("https://www.example.com/xbrl/abc-2024.xsd#abc_Widgets", ACCN),
    ("https://www.example.com/us-gaap/2024/elts/us-gaap-2024.xsd#us-gaap_Revenues", ACCN),
    ("abc-20240630.xsd#abc_Widgets", ACCN),
])
def test_version_by_known_taxonomy(href, version):
    tag, got = _tag_version(href, ACCN)
    assert got == version and tag == href.rsplit("_", 1)[1]


def test_several_presentation_linkbases_pick_the_rendered_one():
    index = json.loads((FILING_DIR / "index.json").read_text())
    index["directory"]["item"].append({"name": "aaa-20240630_pre.xml", "type": "text.gif", "size": "1"})
    sec = FakeSec(_routes(**{"index.json": json.dumps(index).encode()}))
    assert filing_positions(REF, sec)
    assert sec.calls[-1] == f"{FOLDER}/{PRE}"
    # Without the linkbase in InputFiles, the stem of the rendered schema decides.
    summary = (FILING_DIR / "FilingSummary.xml").read_text().replace(f"<File>{PRE}</File>", "")
    sec = FakeSec(_routes(**{"index.json": json.dumps(index).encode(), "FilingSummary.xml": summary.encode()}))
    assert filing_positions(REF, sec) and sec.calls[-1] == f"{FOLDER}/{PRE}"


def test_statement_roles_absent_from_the_linkbase_count_no_rows():
    pre = (FILING_DIR / PRE).read_text().replace("http://www.microsoft.com/role/", "http://other/role/")
    counters = Counter()
    assert filing_positions(REF, FakeSec(_routes(**{PRE: pre.encode()})), counters=counters) == []
    assert counters == Counter({"positions:no_rows": 1})


def _index_with(*names: str, drop_pre: bool = False) -> bytes:
    index = json.loads((FILING_DIR / "index.json").read_text())
    if drop_pre:
        index["directory"]["item"] = [i for i in index["directory"]["item"]
                                      if not i["name"].endswith("_pre.xml")]
    index["directory"]["item"] += [{"name": n, "type": "text.gif", "size": "1"} for n in names]
    return json.dumps(index).encode()


def _summary_without(*names: str) -> bytes:
    summary = (FILING_DIR / "FilingSummary.xml").read_text()
    for n in names:
        summary = summary.replace(f"<File>{n}</File>", "")
    return summary.encode()


def test_listed_presentation_linkbase_missing_falls_back_on_the_schema():
    """A ``*_pre.xml`` listed in index.json but 404 on EDGAR: the embedding schema is read."""
    sec = FakeSec(_routes(**{PRE: None, SCHEMA: _schema_embedding((FILING_DIR / PRE).read_text())}))
    counters = Counter()
    assert filing_positions(REF, sec, counters=counters) == filing_positions(REF, FakeSec(_routes()))
    assert sec.calls[-2:] == [f"{FOLDER}/{PRE}", f"{FOLDER}/{SCHEMA}"] and not counters


def test_presentation_linkbase_without_links_falls_back_on_the_schema():
    empty = ('<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase">'
             '<link:roleRef roleURI="http://x/role"/></link:linkbase>').encode()
    sec = FakeSec(_routes(**{PRE: empty, SCHEMA: _schema_embedding((FILING_DIR / PRE).read_text())}))
    counters = Counter()
    assert filing_positions(REF, sec, counters=counters) == filing_positions(REF, FakeSec(_routes()))
    assert not counters


def test_neither_linkbase_nor_schema_counts_no_presentation_once():
    counters = Counter()
    assert filing_positions(REF, FakeSec(_routes(**{PRE: None})), counters=counters) == []
    assert counters == Counter({"positions:no_presentation": 1})


def test_schema_suffix_is_case_insensitive():
    upper = "MSFT-20240630.XSD"
    index = json.loads(_index_with(upper, drop_pre=True))
    index["directory"]["item"] = [i for i in index["directory"]["item"] if i["name"] != SCHEMA]
    sec = FakeSec(_routes(**{"index.json": json.dumps(index).encode(), PRE: None,
                             "FilingSummary.xml": _summary_without(PRE, SCHEMA),
                             upper: _schema_embedding((FILING_DIR / PRE).read_text())}))
    assert filing_positions(REF, sec) == filing_positions(REF, FakeSec(_routes()))
    assert sec.calls[-1] == f"{FOLDER}/{upper}"


def test_several_schemas_without_input_files_prefer_the_one_with_presentation_links():
    """Without ``InputFiles``, the first schema by name (an exhibit's, say) may embed no
    presentation: the next one that does is read."""
    no_links = _schema_embedding('<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase"/>')
    sec = FakeSec(_routes(**{"index.json": _index_with("aaa-20240630.xsd", drop_pre=True), PRE: None,
                             "FilingSummary.xml": _summary_without(PRE, SCHEMA),
                             "aaa-20240630.xsd": no_links,
                             SCHEMA: _schema_embedding((FILING_DIR / PRE).read_text())}))
    counters = Counter()
    assert filing_positions(REF, sec, counters=counters) == filing_positions(REF, FakeSec(_routes()))
    assert sec.calls[-2:] == [f"{FOLDER}/aaa-20240630.xsd", f"{FOLDER}/{SCHEMA}"] and not counters


def test_a_non_ascii_digit_report_position_counts_malformed():
    summary = (FILING_DIR / "FilingSummary.xml").read_text().replace(
        "<HtmlFileName>R4.htm</HtmlFileName>", "<HtmlFileName>page.htm</HtmlFileName>").replace(
        "<Position>4</Position>", "<Position>²</Position>")
    counters = Counter()
    assert filing_positions(REF, FakeSec(_routes(**{"FilingSummary.xml": summary.encode()})),
                            counters=counters) == []
    assert counters == Counter({"positions:malformed": 1})


def test_an_unparseable_locator_url_counts_malformed():
    pre = (FILING_DIR / PRE).read_text().replace("https://xbrl.fasb.org/", "http://[bad/")
    counters = Counter()
    assert filing_positions(REF, FakeSec(_routes(**{PRE: pre.encode()})), counters=counters) == []
    assert counters == Counter({"positions:malformed": 1})


def test_a_malformed_schema_does_not_hide_a_later_valid_one():
    sec = FakeSec(_routes(**{"index.json": _index_with("aaa-20240630.xsd", drop_pre=True), PRE: None,
                             "FilingSummary.xml": _summary_without(PRE, SCHEMA),
                             "aaa-20240630.xsd": b"<xsd:schema><unclosed>",
                             SCHEMA: _schema_embedding((FILING_DIR / PRE).read_text())}))
    counters = Counter()
    assert filing_positions(REF, sec, counters=counters) == filing_positions(REF, FakeSec(_routes()))
    assert sec.calls[-2:] == [f"{FOLDER}/aaa-20240630.xsd", f"{FOLDER}/{SCHEMA}"] and not counters


def test_only_malformed_presentation_documents_count_malformed_once():
    sec = FakeSec(_routes(**{PRE: b"<link:linkbase><unclosed>", SCHEMA: b"<xsd:schema><unclosed>"}))
    counters = Counter()
    assert filing_positions(REF, sec, counters=counters) == []
    assert counters == Counter({"positions:malformed": 1})


def test_schema_fallback_tries_at_most_max_schemas():
    no_links = _schema_embedding('<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase"/>')
    extra = ("a1.xsd", "a2.xsd", "a3.xsd")
    routes = _routes(**{"index.json": _index_with(*extra, drop_pre=True), PRE: None,
                        "FilingSummary.xml": _summary_without(PRE, SCHEMA),
                        **{n: no_links for n in extra},
                        SCHEMA: _schema_embedding((FILING_DIR / PRE).read_text())})
    sec, counters = FakeSec(routes), Counter()
    assert filing_positions(REF, sec, counters=counters, max_schemas=2) == []
    assert sec.calls[2:] == [f"{FOLDER}/a1.xsd", f"{FOLDER}/a2.xsd"]
    assert counters == Counter({"positions:no_presentation": 1})
    assert filing_positions(REF, FakeSec(routes)) == filing_positions(REF, FakeSec(_routes()))
    assert MAX_SCHEMAS == 4


def test_input_files_match_names_case_insensitively():
    original = (FILING_DIR / "FilingSummary.xml").read_text()
    # only the linkbase listed (upper-cased): the schema-stem rule cannot pick it instead
    summary = original.replace(f"<File>{PRE}</File>", f"<File>{PRE.upper()}</File>").replace(
        f"<File>{SCHEMA}</File>", "")
    sec = FakeSec(_routes(**{"index.json": _index_with("aaa-20240630_pre.xml"),
                             "FilingSummary.xml": summary.encode()}))
    assert filing_positions(REF, sec) and sec.calls[2:] == [f"{FOLDER}/{PRE}"]
    summary = original.replace(f"<File>{PRE}</File>", "").replace(
        f"<File>{SCHEMA}</File>", f"<File>{SCHEMA.upper()}</File>")
    # the listed schema (upper-cased in InputFiles) is tried before an unlisted one by name
    sec = FakeSec(_routes(**{"index.json": _index_with("aaa-20240630.xsd", drop_pre=True), PRE: None,
                             "FilingSummary.xml": summary.encode(),
                             SCHEMA: _schema_embedding((FILING_DIR / PRE).read_text())}))
    assert filing_positions(REF, sec) and sec.calls[2:] == [f"{FOLDER}/{SCHEMA}"]


def test_index_names_with_a_path_component_are_never_requested():
    unsafe = ("..aaa_pre.xml", "sub/aaa_pre.xml", "../aaa.xsd", "a/b.xsd")
    sec = FakeSec(_routes(**{"index.json": _index_with(*unsafe, drop_pre=True), PRE: None,
                             "FilingSummary.xml": _summary_without(PRE, SCHEMA),
                             SCHEMA: _schema_embedding((FILING_DIR / PRE).read_text())}))
    assert filing_positions(REF, sec) == filing_positions(REF, FakeSec(_routes()))
    assert sec.calls[2:] == [f"{FOLDER}/{SCHEMA}"]
