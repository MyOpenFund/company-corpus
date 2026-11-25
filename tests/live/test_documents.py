import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from company_corpus.live.documents import (
    SECTION_RULES,
    DocumentTooLarge,
    FilingDocument,
    PrimaryDocumentNotFound,
    UnsupportedDocument,
    filing_document,
    find_sections,
    html_to_markdown,
    priority_sections,
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


def test_priority_sections_per_form_family():
    assert priority_sections("10-K") == ("Item 7",) == priority_sections("10-K/A")
    assert priority_sections("10-Q") == ("Part I Item 2",)
    assert priority_sections("20-F") == ("Item 5",)
    assert priority_sections("40-F") == () == priority_sections("8-K")
    for rules in SECTION_RULES.values():
        assert set(rules.priority) <= set(rules.emitted)


def test_10q_sections_with_omitted_part_ii_items():
    ref = replace(REF, form="10-Q", primary_document="exco-10q.htm")
    doc = _doc("tenq.htm", ref)
    assert [s.name for s in doc.sections] == [
        "Part I Item 1", "Part I Item 2", "Part I Item 3", "Part I Item 4", "Part II Item 1A"]
    mdna = _text(doc, "Part I Item 2")
    assert "Liquidity and Capital Resources" in mdna and "no material changes" not in mdna
    assert mdna.startswith("ITEM")  # the body heading, not the unlinked TOC row
    item4 = _text(doc, "Part I Item 4")
    assert "effective" in item4 and "PART II" not in item4  # stops at the Part II heading
    assert "UNREGISTERED" not in _text(doc, "Part II Item 1A")
    assert "HIDDENFACT10Q" not in doc.markdown


_BODY = "Body text long enough to be a real section rather than a table-of-contents entry, " * 2


def _toc_rows(*items):
    return "|  |  |  |\n| --- | --- | --- |\n" + "".join(f"| Item {k}. | Title {k} | {n} |\n"
                                                    for n, k in enumerate(items, start=3))


def test_unlinked_toc_with_a_combined_body_heading():
    md = (_toc_rows("1", "1A", "7", "7A", "8") + "\n"
          f"Item 1. Business\n\n{_BODY}\n\nItem 1A. Risk Factors\n\n{_BODY}\n\n"
          f"Items 7 and 7A. MD&A and Market Risk\n\n{_BODY}\n\nItem 8. Statements\n\n{_BODY}\n")
    s = {x.name: x for x in find_sections(md, "10-K")}
    assert list(s) == ["Item 1", "Item 1A", "Item 7", "Item 8"]
    assert md[s["Item 1"].start:].startswith("Item 1. Business")
    assert md[s["Item 7"].start:].startswith("Items 7 and 7A.")
    assert s["Item 7"].end == s["Item 8"].start


def test_unlinked_toc_with_a_missing_body_heading():
    md = (_toc_rows("1", "1A", "7", "7A", "8") + "\n"
          f"Item 1. Business\n\n{_BODY}\n\nItem 1A. Risk Factors\n\n{_BODY}\n\n"
          f"Item 7. MD&A\n\n{_BODY}\n\nItem 8. Statements\n\n{_BODY}\n")
    s = {x.name: x for x in find_sections(md, "10-K")}
    assert list(s) == ["Item 1", "Item 1A", "Item 7", "Item 8"]
    assert all(not md[x.start:].startswith("|") for x in s.values())


def test_short_body_items_are_not_a_table_of_contents():
    md = (f"Item 1. Business\n\n{_BODY}\n\nItem 1B. Unresolved\n\nNone.\n\nItem 1C. Cyber\n\n{_BODY}\n\n"
          "Item 3. Legal\n\nNone.\n\nItem 4. Mine Safety\n\nNot applicable.\n\n"
          f"Item 7. MD&A\n\n{_BODY}\n")
    assert [s.name for s in find_sections(md, "10-K")] == ["Item 1", "Item 7"]


def test_anchor_linked_heading_outside_a_table_is_kept():
    md = f"| [Item 7.](#i7) | MD&A | 34 |\n\n[Item 7. MD&A](#toc)\n\n{_BODY}\n\nItem 8. Statements\n\nx\n"
    (s7, _s8) = find_sections(md, "10-K")
    assert md[s7.start:].startswith("[Item 7. MD&A](#toc)")


def test_item_split_by_a_line_break():
    md = html_to_markdown(b"<p><b>Item<br/>7.</b> MD&amp;A</p><p>body</p><p><b>ITEM 8.</b></p><p>x</p>")
    (s7, _s8) = find_sections(md, "10-K")
    assert s7.name == "Item 7" and "body" in md[s7.start:s7.end]


def test_hidden_attribute_and_visibility_hidden_are_dropped():
    md = html_to_markdown(b'<p>shown</p><div hidden>HIDDEN_ATTR</div>'
                          b'<p style="visibility : Hidden">HIDDEN_VIS</p><p style="color:red">kept</p>')
    assert "HIDDEN_ATTR" not in md and "HIDDEN_VIS" not in md and "shown" in md and "kept" in md


def test_encoding_declared_utf8_then_cp1252():
    assert "caf’" in html_to_markdown(b"<p>caf\x92</p>")  # undeclared, not UTF-8: cp1252
    assert "café" in html_to_markdown("<p>café</p>".encode())  # undeclared UTF-8
    latin = b'<html><head><meta charset="iso-8859-1"></head><body><p>caf\xe9</p></body></html>'
    assert "café" in html_to_markdown(latin)


def test_non_html_primary_document_is_refused_unfetched():
    sec = FakeSec({})
    with pytest.raises(UnsupportedDocument):
        filing_document(replace(REF, primary_document="exco-10k.txt"), sec)
    assert sec.calls == []


def test_document_too_large_is_refused_before_conversion():
    body = (DOCS / "tenk.htm").read_bytes()
    sec = FakeSec({f"{FOLDER}/{PRIMARY}": body})
    with pytest.raises(DocumentTooLarge):
        filing_document(REF, sec, max_html_bytes=len(body) - 1)
    assert filing_document(REF, sec, max_html_bytes=len(body)).sections


def test_reduction_drops_presentation_attributes_and_keeps_links_and_spans():
    md = html_to_markdown(b'<table><tr><td colspan="2" style="width:10%"><span class="x">A</span>'
                          b'<span></span></td></tr><tr><td>1</td><td><a href="ex21.htm" style="c">2</a></td>'
                          b'</tr></table>')
    assert "| A |" in md.replace("  ", " ") and "[2](ex21.htm)" in md and "style" not in md


@pytest.mark.parametrize("fixture", ["tenk_shell.htm", "tenk_smaller.htm"])
def test_short_consecutive_body_items_without_toc_keep_their_headings(fixture):
    # Items 2-6 (and 1A/1C for a smaller reporting company) one line each, PART II between them
    doc = _doc(fixture, replace(REF, primary_document="exco-10k.htm"))
    assert [s.name for s in doc.sections] == ["Item 1", "Item 1A", "Item 7", "Item 7A", "Item 8"]
    assert _text(doc, "Item 7").startswith("**Item 7.") and "SHELLMDNA" in _text(doc, "Item 7")
    expected = "Not required" if fixture == "tenk_smaller.htm" else "We describe this matter"
    assert expected in _text(doc, "Item 1A")


def test_10q_short_part_ii_items_without_toc_keep_their_headings():
    doc = _doc("tenq_short_part2.htm", replace(REF, form="10-Q", primary_document="exco-10q.htm"))
    assert [s.name for s in doc.sections] == [
        "Part I Item 1", "Part I Item 2", "Part I Item 3", "Part I Item 4", "Part II Item 1A"]
    item4 = _text(doc, "Part I Item 4")
    assert "QCONTROLS" in item4 and "PART II" not in item4
    assert "no material changes" in _text(doc, "Part II Item 1A")


@pytest.mark.parametrize("line", ["Item 7, Management's Discussion, is incorporated by reference.",
                                  "Part I, Item 1A of our Annual Report describes these risks."])
def test_comma_after_the_key_is_prose(line):
    md = f"{line}\n\nItem 7A. Market risk\n\nrisk\n"
    assert [s.name for s in find_sections(md, "10-K")] == ["Item 7A"]
    assert find_sections(f"{line}\n\nPART II\n\nItem 1A. Risks\n\nx\n", "10-Q")[-1].name == "Part II Item 1A"


@pytest.mark.parametrize("html", [b"", b"   \n", b"<!-- only a comment -->"])
def test_empty_html_gives_empty_markdown(html):
    assert html_to_markdown(html) == ""


def test_ordered_list_start_is_kept():
    assert "5. fifth" in html_to_markdown(b'<ol start="5" style="x"><li>fifth</li></ol>')


def test_wrong_declared_charset_falls_back():
    wrong = b'<html><head><meta charset="utf-8"></head><body><p>caf\x92</p></body></html>'
    assert "caf’" in html_to_markdown(wrong)


def test_toc_cut_by_sub_entries_stays_a_toc():
    rows = _toc_rows("1", "1A", "7", "7A", "8").rstrip("\n")
    subs = "".join(f"\n|  | Consolidated statement number {i} of the Company | {50 + i} |" for i in range(6))
    tail = "".join(f"\n| Item {k}. | Title {k} | 99 |" for k in ("9", "9A", "10", "11", "15"))
    md = (rows + subs + tail + "\n\n"
          f"Item 1. Business\n\n{_BODY}\n\nItem 1A. Risk Factors\n\n{_BODY}\n\n"
          f"Item 7. MD&A\n\n{_BODY}\n\nItem 8. Statements\n\n{_BODY}\n")
    s = {x.name: x for x in find_sections(md, "10-K")}
    assert list(s) == ["Item 1", "Item 1A", "Item 7", "Item 8"]
    assert all(not md[x.start:].startswith("|") for x in s.values())


def test_items_and_prose_is_not_a_heading():
    md = f"Items 7 and 8 of this report are amended.\n\nItem 8. Statements\n\n{_BODY}\n"
    assert [s.name for s in find_sections(md, "10-K")] == ["Item 8"]


_FULL = "1 1A 1B 1C 2 3 4 5 6 7 7A 8 9 9A 9B 9C 10 11 12 13 14 15".split()


def _body(keys, short=()):
    return "".join(f"Item {k}. Title {k}\n\n" + ("None." if k in short else _BODY) + "\n\n" for k in keys)


def _names_and_places(md, form="10-K"):
    return [(s.name, md[s.start]) for s in find_sections(md, form)]


def test_trailing_cross_reference_index_does_not_take_the_sections():
    short = ("1B", "1C", "2", "3", "4", "5", "6", "9", "9B", "9C", "10", "11", "12", "13", "14")
    md = _body(_FULL, short) + "CROSS-REFERENCE INDEX\n\n" + _toc_rows(*_FULL)
    assert _names_and_places(md) == [("Item 1", "I"), ("Item 1A", "I"), ("Item 7", "I"), ("Item 7A", "I"),
                                     ("Item 8", "I")]


def test_front_toc_with_few_body_headings_keeps_the_body_ones():
    md = _toc_rows(*_FULL) + "\n" + _body(("1", "1A", "7", "7A", "8"))
    assert _names_and_places(md) == [("Item 1", "I"), ("Item 1A", "I"), ("Item 7", "I"), ("Item 7A", "I"),
                                     ("Item 8", "I")]


def test_front_toc_without_body_headings_gives_no_sections():
    md = _toc_rows(*_FULL) + "\n" + (_BODY + "\n\n") * 5
    assert find_sections(md, "10-K") == ()


def test_unexplained_dense_run_gives_no_sections_rather_than_wrong_ones():
    # a plain-line TOC (not a table) that the body repeats for fewer than half of its items
    toc = "".join(f"Item {k}. Title {k} {n}\n\n" for n, k in enumerate(_FULL, start=3))
    md = toc + "INTRODUCTION " + _BODY + "\n\n" + _body(("1", "1A", "7"))
    assert find_sections(md, "10-K") == ()


def test_10q_toc_part_does_not_carry_into_a_body_without_part_i():
    toc = ("|  |  |  |\n| --- | --- | --- |\n| PART I. FINANCIAL INFORMATION |  |  |\n"
           + "".join(f"| Item {k}. | Title {k} | 3 |\n" for k in ("1", "2", "3", "4"))
           + "| PART II. OTHER INFORMATION |  |  |\n"
           + "".join(f"| Item {k}. | Title {k} | 30 |\n" for k in ("1", "1A", "6")))
    md = toc + "\n" + _body(("1", "2", "3", "4")) + "PART II. OTHER INFORMATION\n\n" + _body(("1", "1A", "6"))
    assert _names_and_places(md, "10-Q") == [
        ("Part I Item 1", "I"), ("Part I Item 2", "I"), ("Part I Item 3", "I"), ("Part I Item 4", "I"),
        ("Part II Item 1A", "I")]


def test_10q_toc_with_sub_entries_stays_one_table():
    toc = ("|  |  |  |\n| --- | --- | --- |\n| PART I |  |  |\n| Item 1. | Financial Statements | 3 |\n"
           + "".join(f"|  | Condensed consolidated statement number {i} of the company | {i} |\n" for i in range(5))
           + "".join(f"| Item {k}. | Title {k} | 9 |\n" for k in ("2", "3", "4"))
           + "| PART II |  |  |\n" + "".join(f"| Item {k}. | Title {k} | 30 |\n" for k in ("1", "1A", "6")))
    md = toc + "\nPART I\n\n" + _body(("1", "2", "3", "4")) + "PART II\n\n" + _body(("1", "1A", "6"))
    assert [p for _, p in _names_and_places(md, "10-Q")] == ["I"] * 5


def test_an_appendix_repeating_earlier_items_ends_the_last_section():
    md = _toc_rows("1", "1A", "1B", "1C", "2", "3", "4") + "\n" + _body(("5", "6", "7", "7A", "8")) + _body(("1", "1A"))
    s = {x.name: x for x in find_sections(md, "10-K")}
    assert list(s) == ["Item 7", "Item 7A", "Item 8"]
    assert md[s["Item 8"].end:].startswith("Item 1. Title 1")


def test_combined_part_and_item_heading():
    md = f"PART I \u2014 ITEM 1. BUSINESS\n\n{_BODY}\n\nPART II \u2014 ITEM 7. MD&A\n\n{_BODY}\n"
    assert [s.name for s in find_sections(md, "10-K")] == ["Item 1", "Item 7"]


def test_linked_toc_anchors_are_authoritative():
    doc = _doc("tenk_anchored.htm", replace(REF, primary_document="exco-10k.htm"))
    assert [s.name for s in doc.sections] == ["Item 1", "Item 1A", "Item 7", "Item 7A", "Item 8"]
    for name, marker in (("Item 1", "ANCHOR_BUSINESS"), ("Item 7", "ANCHOR_MDNA"), ("Item 7A", "ANCHOR_MARKET"),
                         ("Item 8", "ANCHOR_FS")):
        text = _text(doc, name).replace("\\_", "_")
        assert marker in text and text.count("ANCHOR_") == 1
    assert "zqanchor" not in doc.markdown
    assert _text(doc, "Item 7").startswith("**WHAT MANAGEMENT THINKS**")
    assert "ACCOUNTANTS" not in _text(doc, "Item 8")
    # without the anchors, the body has no Item headings: no sections rather than TOC rows
    assert find_sections(doc.markdown, "10-K") == ()


def test_unresolved_toc_link_falls_back_to_the_heuristics():
    md = _toc_rows("1") + "| [Item 7.](#gone) | MD&A | 9 |\n\n" + _body(("1", "7", "8"))
    assert find_sections(md, "10-K", anchors={"other": 0}) == find_sections(md, "10-K")
    assert [s.name for s in find_sections(md, "10-K", anchors={"other": 0})] == ["Item 1", "Item 7", "Item 8"]


def test_toc_classification_is_linear():
    md = "".join(f"Item {k}. Title {k} 3\n\n" for _ in range(8000 // len(_FULL) + 1) for k in _FULL)
    md = "\n\n".join(md.split("\n\n")[:8000])
    started = time.perf_counter()
    find_sections(md, "10-K")
    assert time.perf_counter() - started < 5.0


@pytest.mark.parametrize("start", ["\u00bd", "\u00b2", "abc"])
def test_non_integer_list_start_is_dropped(start):
    assert html_to_markdown(f'<ol start="{start}"><li>x</li></ol>'.encode()) == "1. x"


def test_title_attribute_is_dropped():
    assert html_to_markdown(b'<p><a href="a.htm" title="T">t</a></p>') == "[t](a.htm)"


def test_declared_latin1_is_windows_1252():
    html = b'<html><head><meta charset="iso-8859-1"></head><body><p>\x93q\x94 caf\xe9</p></body></html>'
    assert "\u201cq\u201d caf\u00e9" in html_to_markdown(html)
