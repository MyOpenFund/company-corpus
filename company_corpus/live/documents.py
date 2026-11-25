"""Filing primary documents as Markdown, with their Item sections (live, no-vault mode).

Only the primary document of a filing is fetched, never an exhibit. The EDGAR HTML is cleaned
(the inline-XBRL ``ix:header`` block and every ``display:none`` element are dropped), then
converted by MarkItDown; tables come out as pipe tables.

Sections are found on the Markdown output, so ``Section.start``/``Section.end`` are offsets into
``FilingDocument.markdown`` and a consumer cuts by offsets. The rules are data
(:data:`SECTION_RULES`, one entry per form family) so they can be tuned on real filings:

* a heading is a line that, once Markdown markup, table pipes and non-breaking spaces are
  removed, starts with ``Item <key>`` followed by punctuation, the end of the line or a
  capitalised title (``Item 7 is not amended`` is prose), and is at most
  ``max_heading_chars`` long; a line linking to an in-document anchor (a table-of-contents
  entry such as ``[Item 7.](#item_7)``) is not a heading when ``skip_anchor_links``;
* the items of a form are walked from the last to the first: an item's heading is its last
  occurrence before the heading chosen for the next item, so the table-of-contents
  occurrences (followed by the other TOC entries) lose to the body ones; when the item is
  repeated with no other item in between (running page headers), the first of that run wins;
* ``Section.end`` is the start of the next detected item heading (emitted or not: Item 1A
  stops at Item 1B) or the end of the document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from markitdown.converters import HtmlConverter

from .filings import FilingRef
from .sec import SecGet

#: Sections the indexer embeds first (MD&A: Item 7 of a 10-K, Item 5 of a 20-F).
PRIORITY_SECTIONS: tuple[str, ...] = ("Item 7", "Item 5")


class PrimaryDocumentNotFound(LookupError):
    """The filing index lists no HTML document whose type is the filing's form."""


@dataclass(frozen=True)
class Section:
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class FilingDocument:
    ref: FilingRef
    markdown: str
    sections: tuple[Section, ...]


@dataclass(frozen=True)
class SectionRules:
    """``positions``: every item of the form in document order, as ``(key, section name)``;
    a name of ``None`` marks an item that only bounds its neighbours. A key may repeat (the
    Parts of a 10-Q restart their numbering)."""

    positions: tuple[tuple[str, str | None], ...]
    max_heading_chars: int
    skip_anchor_links: bool = True

    @property
    def emitted(self) -> tuple[str, ...]:
        return tuple(name for _, name in self.positions if name is not None)


def _items(keys: str, emitted: dict[str, str]) -> tuple[tuple[str, str | None], ...]:
    return tuple((k, emitted.get(k)) for k in keys.split())


_MAX_HEADING_CHARS = 300  # longest EDGAR Item title rendered with its table-row pipes, with margin

SECTION_RULES: dict[str, SectionRules] = {
    "10-K": SectionRules(
        positions=_items("1 1A 1B 1C 2 3 4 5 6 7 7A 8 9 9A 9B 9C 10 11 12 13 14 15 16",
                         {k: f"Item {k}" for k in ("1", "1A", "7", "7A", "8")}),
        max_heading_chars=_MAX_HEADING_CHARS),
    "20-F": SectionRules(
        positions=_items("1 2 3 4 4A 5 6 7 8 9 10 11 12 13 14 15 16 16A 16B 16C 16D 16E 16F 16G "
                         "16H 16I 16J 16K 17 18 19",
                         {k: f"Item {k}" for k in ("5", "18")}),
        max_heading_chars=_MAX_HEADING_CHARS),
    # Part I (1-4) then Part II (1, 1A, 2-6): only Part I and the Part II risk factors are named.
    "10-Q": SectionRules(
        positions=(("1", "Item 1"), ("2", "Item 2"), ("3", "Item 3"), ("4", "Item 4"),
                   ("1", None), ("1A", "Item 1A"), ("2", None), ("3", None), ("4", None),
                   ("5", None), ("6", None)),
        max_heading_chars=_MAX_HEADING_CHARS),
}

#: Base form (``/A`` removed) -> key of :data:`SECTION_RULES`.
FORM_FAMILIES: dict[str, str] = {
    "10-K": "10-K", "10-KT": "10-K", "10-K405": "10-K",
    "10-Q": "10-Q", "10-QT": "10-Q",
    "20-F": "20-F",
}

_HEADING = re.compile(r"^(?i:item)\s*(\d{1,2}[A-Za-z]?)(?![0-9A-Za-z])(?:\s*[.:\-–—]|\s*$|\s+[A-Z(])")
_MARKUP = re.compile(r"[*_|#>\\]")
_SPACES = re.compile(r"\s+")
_ANCHOR_LINK = re.compile(r"\]\(#")
_DISPLAY_NONE = re.compile(r"display\s*:\s*none", re.IGNORECASE)


def _rules_for(form: str) -> SectionRules | None:
    base = form.upper().strip().removesuffix("/A")
    family = FORM_FAMILIES.get(base)
    return SECTION_RULES.get(family) if family else None


def _candidates(markdown: str, rules: SectionRules, keys: set[str]) -> list[tuple[int, str]]:
    found, offset = [], 0
    for line in markdown.splitlines(keepends=True):
        start, offset = offset, offset + len(line)
        if rules.skip_anchor_links and _ANCHOR_LINK.search(line):
            continue
        text = _SPACES.sub(" ", _MARKUP.sub(" ", line.replace("\xa0", " "))).strip()
        if not text or len(text) > rules.max_heading_chars:
            continue
        m = _HEADING.match(text)
        if m and m.group(1).upper() in keys:
            found.append((start, m.group(1).upper()))
    return found


def find_sections(markdown: str, form: str, *, rules: SectionRules | None = None) -> tuple[Section, ...]:
    """Item sections of ``markdown`` under ``rules`` (default: the rules of ``form``'s family;
    a form without rules has no sections)."""
    rules = rules if rules is not None else _rules_for(form)
    if rules is None:
        return ()
    cands = _candidates(markdown, rules, {k for k, _ in rules.positions})
    chosen: list[tuple[int, str | None]] = []  # (offset, name), built last item first
    bound = len(markdown) + 1
    for key, name in reversed(rules.positions):
        i = max((j for j, (off, k) in enumerate(cands) if k == key and off < bound), default=None)
        if i is None:
            continue
        while i > 0 and cands[i - 1][1] == key:  # running headers repeat the item: the run's first
            i -= 1
        bound = cands[i][0]
        chosen.append((bound, name))
    chosen.reverse()
    sections = []
    for n, (start, name) in enumerate(chosen):
        if name is not None:
            end = chosen[n + 1][0] if n + 1 < len(chosen) else len(markdown)
            sections.append(Section(name=name, start=start, end=end))
    return tuple(sections)


def html_to_markdown(html: bytes) -> str:
    """EDGAR HTML -> Markdown, without the hidden inline-XBRL header or ``display:none`` content."""
    soup = BeautifulSoup(html, "html.parser")
    hidden = soup.find_all("ix:header") + soup.find_all(style=_DISPLAY_NONE)
    for el in hidden:
        if not el.decomposed:
            el.decompose()
    return HtmlConverter().convert_string(str(soup)).markdown


def _cell_text(cell) -> str:
    return _SPACES.sub(" ", cell.get_text(" ").replace("\xa0", " ")).strip()


def primary_document_name(index_html: bytes, form: str) -> str:
    """The primary document as EDGAR lists it in the filing index (``<accession>-index.html``):
    the first row of the "Document Format Files" table whose Type is the filing's form and
    whose document is HTML. Exhibits (``EX-*``) never match. The folder ``index.json`` is not
    used: it carries no document type."""
    soup = BeautifulSoup(index_html, "html.parser")
    wanted = form.upper().strip()
    for table in soup.find_all("table", class_="tableFile"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [_cell_text(c).lower() for c in rows[0].find_all(["th", "td"])]
        if "document" not in header or "type" not in header:
            continue
        doc_col, type_col = header.index("document"), header.index("type")
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) <= max(doc_col, type_col) or _cell_text(cells[type_col]).upper() != wanted:
                continue
            link = cells[doc_col].find("a")
            name = _cell_text(link) if link is not None else ""
            if (name and "/" not in name and ".." not in name
                    and name.lower().endswith((".htm", ".html"))):
                return name
    raise PrimaryDocumentNotFound(f"no {form} HTML document in the filing index")


def filing_document(ref: FilingRef, sec: SecGet) -> FilingDocument:
    """The primary document of ``ref`` as Markdown, with its Item sections. Filing documents
    are immutable: every fetch is made with ``max_age_s=None``."""
    name = ref.primary_document
    if not name:
        index = sec.get_bytes(f"{ref.url}/{ref.accession}-index.html", max_age_s=None)
        name = primary_document_name(index, ref.form)
    markdown = html_to_markdown(sec.get_bytes(f"{ref.url}/{name}", max_age_s=None))
    return FilingDocument(ref=ref, markdown=markdown, sections=find_sections(markdown, ref.form))
