"""Filing primary documents as Markdown, with their Item sections (live, no-vault mode).

Only the primary document of a filing is fetched, never an exhibit, and only an HTML one.

**Conversion.** The bytes are decoded with the declared charset (``<meta charset>`` or the XML
declaration); with none, or when the declared one fails, UTF-8, falling back to cp1252 when
UTF-8 fails. Empty or unparseable HTML gives an empty Markdown. Documents larger than
``max_html_bytes`` are refused (:class:`DocumentTooLarge`) before any parsing. Measured worst
case under the default 20 MB cap: about 45 s and over 1 GB on nested layout tables (to be
re-measured on real filings). The HTML is then
cleaned and reduced before MarkItDown, which bounds its cost on table-dense filings:

* hidden content is dropped: the inline-XBRL ``ix:header`` block, elements with the ``hidden``
  attribute, and elements whose inline ``style`` has ``display:none`` or ``visibility:hidden``.
  Known limit: hiding through a CSS class (a stylesheet rule) is not detected;
* inline-XBRL tags (``ix:*``), ``span`` and ``font`` are unwrapped (their text is kept), and
  every attribute other than ``href``, ``colspan``, ``rowspan``, ``src``, ``alt``, ``start`` and
  ``title`` is dropped.

MarkItDown's HTML converter then renders tables as pipe tables.

**Sections** are found on the Markdown output, so ``Section.start``/``Section.end`` are offsets
into ``FilingDocument.markdown``. The rules are data (:data:`SECTION_RULES`, one entry per form
family, every threshold a field) so they can be tuned on real filings:

* a heading is a line that, once Markdown markup, table pipes and non-breaking spaces are
  removed, starts with ``Item <key>`` (or ``Items <key> and <key>``, which counts as the first
  item) followed by punctuation, the end of the line or a capitalised title (``Item 7 is not
  amended`` is prose), and is at most ``max_heading_chars`` long. ``Item`` alone on a line is
  joined with the next non-blank line (``Item<br>7.``). ``Part <I-IV>`` lines are found the same
  way;
* table of contents: consecutive heading lines (items and parts) separated by at most
  ``toc_max_gap_chars`` characters of text, in document order (a run restarts when the order
  goes back), form a run. A run of at least ``toc_min_run`` lines is a table of contents, and
  none of its lines is a heading, only when the body repeats it: at least
  ``toc_min_later_share`` of its items (an item is its position under the Part in effect) occur
  again after the run. Consecutive short body items (``None.``, ``[Reserved]``) have no later
  occurrence and stay headings. A run that directly follows a table-of-contents run and goes on
  in its item order (a TOC cut by sub-entries) is part of it. A line linking to an in-document anchor inside a pipe table
  (``| [Item 7.](#item_7) | ... |``) is a table-of-contents entry too
  (``skip_table_anchor_links``); a linked heading outside a table is kept;
* the items of a form are walked from the last to the first: an item's heading is its last
  occurrence before the heading chosen for the next item. When the item is repeated with no
  other item in between (running page headers), the first of that run wins;
* families whose item numbers restart in each Part (10-Q) have ``segment_by_part``: the Part
  headings are chosen first (same walk), and each Part's items are looked for inside it. A Part
  whose heading is missing is skipped, except the first one, which then starts the document.
  Their sections are named with the Part (``Part I Item 2``);
* ``Section.end`` is the start of the next detected item heading (emitted or not: Item 1A stops
  at Item 1B), of the next chosen Part heading for a segmented family, or the end of the document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import lxml.etree
import lxml.html
from bs4 import BeautifulSoup
from markitdown.converters import HtmlConverter

from .filings import FilingRef
from .sec import SecGet

#: Default for ``max_html_bytes``: the largest primary document converted.
MAX_HTML_BYTES = 20_000_000

_HTML_SUFFIXES = (".htm", ".html")
_KEPT_ATTRIBUTES = frozenset({"href", "colspan", "rowspan", "src", "alt", "start", "title"})
_UNWRAPPED_TAGS = frozenset({"span", "font"})


class PrimaryDocumentNotFound(LookupError):
    """The filing index lists no HTML document whose type is the filing's form."""


class UnsupportedDocument(ValueError):
    """The primary document is not HTML (``.htm``/``.html``)."""


class DocumentTooLarge(ValueError):
    """The primary document exceeds ``max_html_bytes``; it is not converted."""


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
    """``positions``: every item of the form in document order, as ``(part, key, section name)``;
    ``part`` is ``None`` unless the family is ``segment_by_part``; a name of ``None`` marks an item
    that only bounds its neighbours. ``priority``: the sections an indexer embeds first."""

    positions: tuple[tuple[str | None, str, str | None], ...]
    priority: tuple[str, ...] = ()
    parts: tuple[str, ...] = ()
    segment_by_part: bool = False
    max_heading_chars: int = 300  # longest EDGAR Item title with its table-row pipes, with margin
    toc_min_run: int = 5  # a TOC lists most items; a filing body rarely has 5 near-empty items in a row
    toc_max_gap_chars: int = 80  # text between two TOC lines: a title and a page number at most
    # A dense run is a TOC only when the body repeats it: at least this share of its items
    # occur again after the run (a run of short body items has no later occurrences).
    toc_min_later_share: float = 0.5
    skip_table_anchor_links: bool = True

    @property
    def emitted(self) -> tuple[str, ...]:
        return tuple(name for _, _, name in self.positions if name is not None)


def _items(keys: str, emitted: tuple[str, ...]) -> tuple[tuple[None, str, str | None], ...]:
    return tuple((None, k, f"Item {k}" if k in emitted else None) for k in keys.split())


def _part_items(part: str, keys: str, emitted: tuple[str, ...]) -> tuple[tuple[str, str, str | None], ...]:
    return tuple((part, k, f"Part {part} Item {k}" if k in emitted else None) for k in keys.split())


SECTION_RULES: dict[str, SectionRules] = {
    "10-K": SectionRules(
        positions=_items("1 1A 1B 1C 2 3 4 5 6 7 7A 8 9 9A 9B 9C 10 11 12 13 14 15 16",
                         ("1", "1A", "7", "7A", "8")),
        priority=("Item 7",)),
    "20-F": SectionRules(
        positions=_items("1 2 3 4 4A 5 6 7 8 9 10 11 12 13 14 15 16 16A 16B 16C 16D 16E 16F 16G "
                         "16H 16I 16J 16K 17 18 19", ("5", "18")),
        priority=("Item 5",)),
    # Part I (1-4) then Part II (1, 1A, 2-6): item numbers restart, so sections carry the Part.
    "10-Q": SectionRules(
        positions=(_part_items("I", "1 2 3 4", ("1", "2", "3", "4"))
                   + _part_items("II", "1 1A 2 3 4 5 6", ("1A",))),
        priority=("Part I Item 2",), parts=("I", "II"), segment_by_part=True),
}

#: Base form (``/A`` removed) -> key of :data:`SECTION_RULES`. Other forms (40-F, 8-K, ...)
#: have no sections and no priority.
FORM_FAMILIES: dict[str, str] = {
    "10-K": "10-K", "10-KT": "10-K", "10-K405": "10-K",
    "10-Q": "10-Q", "10-QT": "10-Q",
    "20-F": "20-F",
}

_PART_ORDER = ("I", "II", "III", "IV")
_TAIL = r"(?![0-9A-Za-z])(?:\s*[.:\-–—]|\s*$|\s+[A-Z(]"
_ITEM = re.compile(r"^(?i:items?)\s*(\d{1,2}[A-Za-z]?)" + _TAIL
                   + r"|\s+(?i:and|through|to)\s+\d{1,2}[A-Za-z]?" + _TAIL + r"))")
_PART = re.compile(r"^(?i:part)\s+(IV|I{1,3})" + _TAIL + r")")
_BARE_WORD = re.compile(r"(?i)items?|part")
_MARKUP = re.compile(r"[*_|#>\\[]")
_SPACES = re.compile(r"\s+")
_ANCHOR_LINK = re.compile(r"\]\(#")
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_DECLARED = re.compile(rb"""<\?xml[^>]*encoding\s*=\s*["']([\w.:-]+)|<meta[^>]*charset\s*=\s*["']?([\w.:-]+)""",
                       re.IGNORECASE)


def _rules_for(form: str) -> SectionRules | None:
    family = FORM_FAMILIES.get(form.upper().strip().removesuffix("/A"))
    return SECTION_RULES.get(family) if family else None


def priority_sections(form: str) -> tuple[str, ...]:
    """Sections of ``form`` an indexer embeds first (the MD&A): ``("Item 7",)`` for a 10-K,
    ``("Part I Item 2",)`` for a 10-Q, ``("Item 5",)`` for a 20-F, ``()`` otherwise."""
    rules = _rules_for(form)
    return rules.priority if rules else ()


@dataclass(frozen=True)
class _Cand:
    start: int
    end: int
    kind: str  # "item" or "part"
    key: str
    linked: bool
    in_table: bool


def _norm(text: str) -> str:
    return _SPACES.sub(" ", _MARKUP.sub(" ", text.replace("\xa0", " "))).strip()


def _scan(markdown: str, rules: SectionRules) -> list[_Cand]:
    lines = markdown.splitlines(keepends=True)
    offsets, total = [], 0
    for line in lines:
        offsets.append(total)
        total += len(line)
    found = []
    for i, line in enumerate(lines):
        text = _norm(line)
        if not text:
            continue
        raw, end = line, offsets[i] + len(line)
        if _BARE_WORD.fullmatch(text):  # "Item<br>7." -> join with the next non-blank line
            j = next((j for j in range(i + 1, len(lines)) if _norm(lines[j])), None)
            if j is None:
                continue
            text, raw, end = f"{text} {_norm(lines[j])}", raw + lines[j], offsets[j] + len(lines[j])
        if len(text) > rules.max_heading_chars:
            continue
        for kind, pattern in (("item", _ITEM), ("part", _PART)):
            m = pattern.match(text)
            if m:
                found.append(_Cand(offsets[i], end, kind, m.group(1).upper(),
                                   bool(_ANCHOR_LINK.search(raw)), line.lstrip().startswith("|")))
                break
    return found


def _order(rules: SectionRules, part: str | None, key: str) -> int | None:
    for i, (p, k, _) in enumerate(rules.positions):
        if k == key and (part is None or p is None or p == part):
            return i
    return None


def _toc_lines(markdown: str, cands: list[_Cand], rules: SectionRules) -> set[int]:
    """Indices of ``cands`` in a table-of-contents run (see the module docstring)."""
    # An item's identity is its position under the Part in effect at that point of the
    # document (10-Q: Part I Item 3 and Part II Item 3 differ); a Part line has none.
    ident: list[int | str | None] = []
    doc_part = None
    for c in cands:
        if c.kind == "part":
            doc_part = c.key
            ident.append(None)
        else:
            idx = _order(rules, doc_part, c.key)
            ident.append(idx if idx is not None else c.key)
    runs: list[list[int]] = []
    last_part = last_item = None
    for n, c in enumerate(cands):
        split = not runs or len(_norm(markdown[cands[n - 1].end:c.start])) > rules.toc_max_gap_chars
        order = _PART_ORDER.index(c.key) if c.kind == "part" else ident[n]
        if c.kind == "part":
            split = split or (last_part is not None and order <= last_part)
        elif isinstance(order, int):
            split = split or (last_item is not None and order <= last_item)
        if split:
            runs.append([])
            last_part = last_item = None
        runs[-1].append(n)
        if c.kind == "part":
            last_part = order
        elif isinstance(order, int):
            last_item = order
    toc: set[int] = set()
    previous_last: int | None = None  # last item order of the previous run, when it is a TOC
    for run in runs:
        orders = [ident[n] for n in run if isinstance(ident[n], int)]
        items = {ident[n] for n in run if ident[n] is not None}
        later = {ident[m] for m in range(run[-1] + 1, len(cands)) if ident[m] is not None}
        corroborated = (len(run) >= rules.toc_min_run and bool(items)
                        and len(items & later) >= rules.toc_min_later_share * len(items))
        # a TOC cut by a long gap (sub-entries under an item) goes on in item order
        continued = previous_last is not None and bool(orders) and orders[0] > previous_last
        if corroborated or continued:
            toc.update(run)
            previous_last = orders[-1] if orders else previous_last
        else:
            previous_last = None
    return toc


def _walk(cands: list[_Cand], keys: list[str], lo: int, hi: int) -> list[tuple[int, int]]:
    """``(offset, index in keys)`` chosen for ``keys`` among ``cands`` within ``[lo, hi)``:
    walked from the last key, each the last occurrence before the next one chosen; a run of
    the same key with nothing between keeps its first occurrence."""
    inside = [c for c in cands if lo <= c.start < hi]
    chosen, bound = [], hi
    for pos in range(len(keys) - 1, -1, -1):
        i = max((j for j, c in enumerate(inside) if c.key == keys[pos] and c.start < bound), default=None)
        if i is None:
            continue
        while i > 0 and inside[i - 1].key == keys[pos]:
            i -= 1
        bound = inside[i].start
        chosen.append((bound, pos))
    chosen.reverse()
    return chosen


def find_sections(markdown: str, form: str, *, rules: SectionRules | None = None) -> tuple[Section, ...]:
    """Item sections of ``markdown`` under ``rules`` (default: the rules of ``form``'s family;
    a form without rules has no sections)."""
    rules = rules if rules is not None else _rules_for(form)
    if rules is None:
        return ()
    cands = _scan(markdown, rules)
    toc = _toc_lines(markdown, cands, rules)
    kept = [c for n, c in enumerate(cands) if n not in toc
            and not (rules.skip_table_anchor_links and c.linked and c.in_table)]
    items = [c for c in kept if c.kind == "item"]
    size = len(markdown)
    boundaries: list[tuple[int, str | None]] = []
    if rules.segment_by_part:
        parts = [c for c in kept if c.kind == "part" and c.key in rules.parts]
        starts = {rules.parts[pos]: off for off, pos in _walk(parts, list(rules.parts), 0, size + 1)}
        boundaries += [(off, None) for off in starts.values()]
        for n, part in enumerate(rules.parts):
            later = [starts[p] for p in rules.parts[n + 1:] if p in starts]
            if part in starts:
                lo = starts[part]
            elif n == 0:
                lo = 0
            else:
                continue
            hi = min(later, default=size + 1)
            positions = [(k, name) for p, k, name in rules.positions if p == part]
            boundaries += [(off, positions[pos][1])
                           for off, pos in _walk(items, [k for k, _ in positions], lo, hi)]
    else:
        positions = [(k, name) for _, k, name in rules.positions]
        boundaries += [(off, positions[pos][1])
                       for off, pos in _walk(items, [k for k, _ in positions], 0, size + 1)]
    boundaries.sort(key=lambda b: b[0])
    sections = []
    for n, (start, name) in enumerate(boundaries):
        if name is not None:
            end = next((off for off, _ in boundaries[n + 1:] if off > start), size)
            sections.append(Section(name=name, start=start, end=end))
    return tuple(sections)


def _decode(html: bytes) -> str:
    m = _DECLARED.search(html[:4096])
    if m:
        try:
            return html.decode((m.group(1) or m.group(2)).decode("ascii"))
        except (LookupError, UnicodeDecodeError):
            pass  # unknown codec name or wrong declaration: treated as undeclared
    try:
        return html.decode("utf-8")
    except UnicodeDecodeError:
        return html.decode("cp1252", errors="replace")


def _reduced_html(text: str) -> str:
    """Hidden content removed, inline wrappers unwrapped, presentation attributes dropped
    (lxml: a native parse, much cheaper than a BeautifulSoup tree on multi-MB filings)."""
    parser = lxml.html.HTMLParser(encoding="utf-8", huge_tree=True, remove_comments=True)
    root = lxml.html.document_fromstring(text.encode("utf-8"), parser=parser)
    dropped = [el for el in root.iter() if isinstance(el.tag, str) and (
        el.tag in ("script", "style", "ix:header") or "hidden" in el.attrib
        or _HIDDEN_STYLE.search(el.get("style") or ""))]
    for el in dropped:
        if el.getparent() is not None:
            el.drop_tree()  # keeps the element's tail text
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if (el.tag in _UNWRAPPED_TAGS or el.tag.startswith("ix:")) and el.getparent() is not None:
            el.drop_tag()  # keeps its text and children
            continue
        for name in [k for k in el.attrib if k not in _KEPT_ATTRIBUTES]:
            del el.attrib[name]
    return lxml.html.tostring(root, encoding="unicode")


def html_to_markdown(html: bytes, *, max_html_bytes: int = MAX_HTML_BYTES) -> str:
    """EDGAR HTML -> Markdown, without hidden content (see the module docstring)."""
    if len(html) > max_html_bytes:
        raise DocumentTooLarge(f"{len(html)} bytes > max_html_bytes={max_html_bytes}")
    text = _decode(html)
    if not text.strip():
        return ""
    try:
        reduced = _reduced_html(text)
    except lxml.etree.ParserError:  # nothing parseable (e.g. only a comment)
        return ""
    return HtmlConverter().convert_string(reduced).markdown


def _cell_text(cell) -> str:
    return _SPACES.sub(" ", cell.get_text(" ").replace("\xa0", " ")).strip()


def primary_document_name(index_html: bytes, form: str) -> str:
    """The primary document as EDGAR lists it in the filing index (``<accession>-index.html``):
    the first row of a ``tableFile`` table whose Type is the filing's form and whose document is
    HTML. Exhibits (``EX-*``) never match. The folder ``index.json`` is not used: it carries no
    document type."""
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
            if name and "/" not in name and ".." not in name and name.lower().endswith(_HTML_SUFFIXES):
                return name
    raise PrimaryDocumentNotFound(f"no {form} HTML document in the filing index")


def filing_document(ref: FilingRef, sec: SecGet, *,
                    max_html_bytes: int = MAX_HTML_BYTES) -> FilingDocument:
    """The primary document of ``ref`` as Markdown, with its Item sections. Filing documents
    are immutable: every fetch is made with ``max_age_s=None``. A non-HTML primary document
    raises :class:`UnsupportedDocument` without being fetched; one over ``max_html_bytes``
    raises :class:`DocumentTooLarge` before conversion."""
    name = ref.primary_document
    if name and not name.lower().endswith(_HTML_SUFFIXES):
        raise UnsupportedDocument(f"primary document {name!r} is not HTML")
    if not name:
        index = sec.get_bytes(f"{ref.url}/{ref.accession}-index.html", max_age_s=None)
        name = primary_document_name(index, ref.form)
    html = sec.get_bytes(f"{ref.url}/{name}", max_age_s=None)
    markdown = html_to_markdown(html, max_html_bytes=max_html_bytes)
    return FilingDocument(ref=ref, markdown=markdown, sections=find_sections(markdown, ref.form))
