import json
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from company_corpus.config import Config
from company_corpus.live.filings import FilingRef
from company_corpus.live.positions import classify_report, filing_positions
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
    ("Income Taxes (Details)", (None, False)),
])
def test_classify_report_cases(short_name, expected):
    assert classify_report(short_name, "", "") == expected


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


def test_missing_index_returns_empty():
    counters = Counter()
    assert filing_positions(REF, FakeSec({}), counters=counters) == []
    assert counters["positions:no_index"] == 1


@pytest.mark.parametrize("name, body", [
    ("index.json", b"{not json"),
    ("index.json", b'{"directory": []}'),
    ("FilingSummary.xml", b"<FilingSummary><MyReports>"),
    (PRE, b"<link:linkbase xmlns:link='x'><unclosed>"),
])
def test_malformed_files_give_no_positions_and_a_counter(name, body):
    counters = Counter()
    assert filing_positions(REF, FakeSec(_routes(**{name: body})), counters=counters) == []
    assert counters["positions:malformed"] == 1


def test_doctype_is_refused_before_parsing():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
            b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><FilingSummary>&b;</FilingSummary>')
    external = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
    utf16 = external.decode().replace('version="1.0"', 'version="1.0" encoding="utf-16"').encode("utf-16")
    for body in (bomb, external, utf16, b"\xff\xfe\x00"):
        counters = Counter()
        assert filing_positions(REF, FakeSec(_routes(**{"FilingSummary.xml": body})), counters=counters) == []
        assert counters["positions:malformed"] == 1


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
