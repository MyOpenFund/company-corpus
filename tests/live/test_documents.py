from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from company_corpus.live.documents import (
    PRIORITY_SECTIONS,
    SECTION_RULES,
    FilingDocument,
    PrimaryDocumentNotFound,
    filing_document,
    find_sections,
    html_to_markdown,
)
from company_corpus.live.filings import FilingRef
from company_corpus.live.sec import SecNotFound

DOCS = Path(__file__).resolve().parent.parent / "fixtures" / "live" / "documents"
ACCN = "0000950170-24-087843"
FOLDER = "https://www.sec.gov/Archives/edgar/data/789019/000095017024087843"
PRIMARY = "exco-10k_20240630.htm"
REF = FilingRef(
    cik="0000789019", accession=ACCN, form="10-K", report_date=date(2024, 6, 30),
    filed=date(2024, 7, 30), accepted=datetime(2024, 7, 30, 16, 6, 22, tzinfo=timezone.utc),
    primary_document=PRIMARY, url=FOLDER, doc_id=None)
INDEX_URL = f"{FOLDER}/{ACCN}-index.html"


class FakeSec:
    """Serves ``{url: bytes}``; any other URL is a 404, like EDGAR."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_bytes(self, url, *, max_age_s=None):
        self.calls.append((url, max_age_s))
        if url not in self.routes:
            raise SecNotFound(url)
        return self.routes[url]


def _doc(name: str, ref: FilingRef = REF) -> FilingDocument:
    sec = FakeSec({f"{FOLDER}/{ref.primary_document}": (DOCS / name).read_bytes()})
    return filing_document(ref, sec)


def _text(doc: FilingDocument, name: str) -> str:
    (s,) = [s for s in doc.sections if s.name == name]
    return doc.markdown[s.start:s.end]


def test_10k_sections():
    doc = _doc("tenk.htm")
    names = [s.name for s in doc.sections]
    assert names == ["Item 1", "Item 1A", "Item 7", "Item 7A", "Item 8"]
    starts = [s.start for s in doc.sections]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    item7 = _text(doc, "Item 7")
    assert "liquidity and capital resources" in item7.casefold()
    # not inside the table of contents: the TOC (with its anchor links) is entirely before it
    toc_end = doc.markdown.index("(#item_15)")
    assert min(starts) > toc_end
    assert "](#" not in item7
    assert "MANAGEMENT" in item7.splitlines()[0]
    # Item 7 stops at Item 7A; Item 1A stops at Item 1B (a detected, non-emitted item)
    assert "foreign exchange" not in item7
    assert "foreign exchange" in _text(doc, "Item 7A")
    assert "PROPERTIES" not in _text(doc, "Item 1A")
    assert "Net income" in _text(doc, "Item 8")
    assert "CHANGES IN AND DISAGREEMENTS" not in _text(doc, "Item 8")
    # the prose cross-reference "See Part II, Item 7" is not a heading
    assert "See Part" in _text(doc, "Item 1")


def test_hidden_xbrl_header_and_display_none_do_not_leak():
    md = _doc("tenk.htm").markdown
    assert "HIDDENFACTFY" not in md and "HIDDENSPANTEXT" not in md and "2023-07-01" not in md
    assert "33,145" in md  # inline XBRL facts in the visible text are kept


def test_tables_become_pipe_tables():
    lines = _doc("tenk.htm").markdown.splitlines()
    net = [ln for ln in lines if "Net income" in ln]
    assert net and all(ln.startswith("|") for ln in net)
    assert any("88,136" in ln for ln in net)
    assert any(ln.startswith("| ---") for ln in lines)


def test_no_exhibit_fetched():
    sec = FakeSec({f"{FOLDER}/{PRIMARY}": (DOCS / "tenk.htm").read_bytes()})
    filing_document(REF, sec)
    assert sec.calls == [(f"{FOLDER}/{PRIMARY}", None)]  # primary only, immutable: no TTL


def test_primary_document_from_the_filing_index_when_unknown():
    ref = replace(REF, primary_document=None)
    sec = FakeSec({INDEX_URL: (DOCS / "filing-index.html").read_bytes(),
                   f"{FOLDER}/{PRIMARY}": (DOCS / "tenk.htm").read_bytes()})
    doc = filing_document(ref, sec)
    assert [u for u, _ in sec.calls] == [INDEX_URL, f"{FOLDER}/{PRIMARY}"]
    assert all(age is None for _, age in sec.calls)
    assert "Item 7" in [s.name for s in doc.sections]


def test_primary_document_absent_from_the_filing_index():
    ref = replace(REF, primary_document=None, form="10-Q")
    sec = FakeSec({INDEX_URL: (DOCS / "filing-index.html").read_bytes()})
    with pytest.raises(PrimaryDocumentNotFound):
        filing_document(ref, sec)
    assert [u for u, _ in sec.calls] == [INDEX_URL]  # the EX-21 is never a fallback


def test_20f_sections():
    ref = replace(REF, form="20-F", primary_document="exco-20f.htm")
    doc = _doc("twentyf.htm", ref)
    assert [s.name for s in doc.sections] == ["Item 5", "Item 18"]
    item5 = _text(doc, "Item 5")
    assert "Liquidity and Capital Resources" in item5 and "board has nine" not in item5
    assert "Articles of Association" not in _text(doc, "Item 18")
    assert "HIDDENFACT20F" not in doc.markdown


def test_amendment_without_item_7():
    ref = replace(REF, form="10-K/A", primary_document="exco-10ka.htm")
    doc = _doc("tenk_amendment.htm", ref)
    assert "Item 7" not in [s.name for s in doc.sections]
    assert "Executive Compensation" in doc.markdown


def test_unknown_form_has_no_sections():
    ref = replace(REF, form="8-K", primary_document="exco-8k.htm")
    doc = _doc("tenk.htm", ref)
    assert doc.sections == () and "LIQUIDITY" in doc.markdown


def test_section_end_is_next_detected_item_or_end_of_document():
    md = html_to_markdown((DOCS / "twentyf.htm").read_bytes())
    sections = find_sections(md, "20-F")
    assert sections[-1].end == md.index("**ITEM 19")
    head = md[:md.index("**ITEM 19")]
    assert find_sections(head, "20-F")[-1].end == len(head)


def test_last_heading_occurrence_before_the_next_item_wins():
    # a TOC without links: plain table rows, then the body headings
    md = ("| Item 7. | MD&A | 34 |\n| Item 7A. | Market risk | 51 |\n| Item 8. | Statements | 52 |\n\n"
          "Item 7. MD&A\n\nbody seven\n\nItem 7A. Market risk\n\nbody seven a\n\n"
          "Item 8. Statements\n\nbody eight\n")
    s = {x.name: x for x in find_sections(md, "10-K")}
    assert md[s["Item 7"].start:s["Item 7"].end].strip() == "Item 7. MD&A\n\nbody seven"
    assert md[s["Item 8"].start:].startswith("Item 8. Statements")


def test_rules_are_data():
    assert PRIORITY_SECTIONS == ("Item 7", "Item 5")
    assert {"10-K", "20-F"} <= set(SECTION_RULES)
    emitted = {name for r in SECTION_RULES.values() for name in r.emitted}
    assert {"Item 1", "Item 1A", "Item 7", "Item 7A", "Item 8", "Item 5", "Item 18"} <= emitted


def test_running_page_headers_keep_the_first_heading_of_the_item():
    md = ("Item 7. MD&A\n\nfirst page\n\n---\n\nItem 7\n\nsecond page\n\n"
          "Item 7A. Market risk\n\nrisk\n")
    (s7, s7a) = find_sections(md, "10-K")
    assert s7.start == 0 and "second page" in md[s7.start:s7.end] and s7.end == s7a.start


def test_prose_starting_with_item_is_not_a_heading():
    md = "Item 7 is not amended by this report.\n\nItem 7A. Market risk\n\nrisk\n"
    assert [s.name for s in find_sections(md, "10-K")] == ["Item 7A"]


def test_heading_longer_than_the_rule_is_not_a_heading():
    rules = replace(SECTION_RULES["10-K"], max_heading_chars=10)
    md = "Item 7. MANAGEMENT'S DISCUSSION\n\nItem 7A.\n\nrisk\n"
    assert [s.name for s in find_sections(md, "10-K", rules=rules)] == ["Item 7A"]
