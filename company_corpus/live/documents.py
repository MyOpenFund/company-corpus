"""Filing primary documents as Markdown, with their Item sections (live, no-vault mode).

Only the primary document of a filing is fetched, never an exhibit, and only an HTML one.

**Conversion.** The bytes are decoded with the declared charset (``<meta charset>`` or the XML
declaration; a declared Latin-1 is read as windows-1252 and a declared UTF-16 as UTF-8, as HTML
does, whatever the spelling of the name); with none, or when the declared one fails, UTF-8,
falling back to cp1252 when UTF-8 fails. Empty or unparseable HTML
gives an empty Markdown. Documents larger than ``max_html_bytes`` are refused
(:class:`DocumentTooLarge`) before any parsing. Measured worst case under the default 20 MB cap:
about 45 s and over 1 GB on nested layout tables (to be re-measured on real filings). The HTML is
then cleaned and reduced before MarkItDown, which bounds its cost on table-dense filings:

* hidden content is dropped: the inline-XBRL ``ix:header`` block, elements with the ``hidden``
  attribute, and elements whose inline ``style`` has ``display:none`` or ``visibility:hidden``.
  Known limit: hiding through a CSS class (a stylesheet rule) is not detected;
* the targets of in-document links get a text marker, removed after conversion, which gives
  each anchor id its Markdown offset (start of the line where the target's text begins). The
  Markdown does not depend on the markers: a paragraph holding only markers is removed whole;
* inline-XBRL tags (``ix:*``), ``span`` and ``font`` are unwrapped (their text is kept), and
  every attribute other than ``href``, ``colspan``, ``rowspan``, ``src``, ``alt`` and an
  integer ``start`` is dropped.

MarkItDown's HTML converter then renders tables as pipe tables.

**Sections** are found on the Markdown output, so ``Section.start``/``Section.end`` are offsets
into ``FilingDocument.markdown``. Principle: a wrong section is worse than no section (the whole
document is indexed anyway; sections order the indexing and label hits). The rules are data
(:data:`SECTION_RULES`, one entry per form family, every threshold a field):

* linked table of contents (authoritative): when the TOC's Item lines link to in-document
  anchors that all resolve, in item order, and all after the last linked TOC line (a target
  inside the TOC is a broken link), each listed item starts at its anchor. The items the TOC
  does not link get the boundaries of the heuristics below when those are consistent with the
  anchors (all after the TOC, and in item order once merged); when they are not, or when the
  heuristics give nothing, only the linked items are sections (known limit: a linked item then
  runs to the next anchor, over an item the TOC does not list);
* otherwise, headings: a line that, once Markdown markup, table pipes and non-breaking spaces
  are removed, starts with ``Item <key>`` (or ``Items <key> and <key>``, counted as the first
  item; or ``Part II - Item 7``) followed by punctuation, the end of the line or a capitalised
  title (``Item 7 is not amended`` and ``Item 7, Management's...`` are prose; ``Item 5.02`` is
  another form's numbering, not Item 5; ``Item 5.A`` is Item 5A), at most
  ``max_heading_chars`` long. ``Item`` alone on a line is joined with the next non-blank line
  (``Item<br>7.``). ``Part <I-IV>`` lines are found the same way. A line linking to an anchor
  inside a pipe table is a TOC entry (``skip_table_anchor_links``);
* dense runs: consecutive heading lines at most ``toc_max_gap_chars`` apart (or rows of one
  pipe table), in item order, form a run; runs of at least ``toc_min_run`` lines are classified.
  A run whose lines are mostly rows of one table is a TOC or a cross-reference index, wherever
  it sits. Another run is a TOC when at least ``toc_min_later_share`` of its items occur again
  later outside the tables (a plain-line TOC). A remaining run is body (consecutive short
  items: ``None.``, ``[Reserved]``) only when its items occur nowhere else **and** at least one
  Item heading stands outside every dense run (a body has long items too; absence of a second
  occurrence alone proves nothing). Any other run is unexplained: if a section boundary would
  land on it, **no sections** are returned (so a plain-line TOC over a body without Item
  headings, or a document whose items are all one-liners, has no sections). An item's identity
  is its position under the Part in effect; a Part read inside a table run, or inside a run
  listing at least ``toc_min_part_lines`` Parts (a TOC), does not carry past that run.
  Known limits: a TOC shorter than ``toc_min_run`` lines over a body without Item headings is
  read as headings; so is a plain-line TOC none of whose items has a body heading while the
  body has a heading for an item the TOC does not list;
* the items of a form are walked from the last to the first: an item's heading is its last
  occurrence before the heading chosen for the next item. When the item is repeated with no
  other item in between (running page headers), the first of that run wins;
* families whose item numbers restart in each Part (10-Q) have ``segment_by_part``: the Part
  headings are chosen first (same walk), and each Part's items are looked for inside it. A Part
  whose heading is missing is skipped, except the first one, which then starts the document.
  Their sections are named with the Part (``Part I Item 2``);
* ``Section.end`` is the start of the next chosen boundary (an item, emitted or not: Item 1A
  stops at Item 1B; or a chosen Part heading) or the end of the document. Only the last
  boundary's section is cut short: at the next heading of another item of the form (an appendix
  repeating earlier items). Headings between two boundaries never cut a section: sub-items
  (20-F ``Item 5A``), keys the form does not have, stray Item-like lines.
"""
from __future__ import annotations

import codecs
import re
from collections.abc import Mapping
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
_KEPT_ATTRIBUTES = frozenset({"href", "colspan", "rowspan", "src", "alt", "start"})
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
    # A dense run listing at least this many Part lines is a TOC spanning Parts: the Part in
    # effect is reset after it (a body run crosses one Part heading at most, and keeps its Part).
    toc_min_part_lines: int = 2
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
_TAIL = r"(?![0-9A-Za-z])(?!\.\d)(?:\s*[.:\-–—]|\s*$|\s+[A-Z(]"
_ITEM = re.compile(r"^(?i:items?)\s*(\d{1,2}(?:\.?[A-Za-z])?)" + _TAIL
                   + r"|\s+(?i:and|through|to)\s+\d{1,2}[A-Za-z]?" + _TAIL + r"))")
_PART = re.compile(r"^(?i:part)\s+(IV|I{1,3})" + _TAIL + r")")
_PART_THEN_ITEM = re.compile(r"^(?i:part)\s+(?:IV|I{1,3})\s*[.:,\-–—]?\s*(?=(?i:items?)\s*\d)")
_BARE_WORD = re.compile(r"(?i)items?|part")
_MARKUP = re.compile(r"[*_|#>\\[]")
_SPACES = re.compile(r"\s+")
_ANCHOR_TARGET = re.compile(r"\]\(#([^)\s]+)\)")
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_INTEGER = re.compile(r"-?\d+")
_DECLARED = re.compile(rb"""<\?xml[^>]*encoding\s*=\s*["']([\w.:-]+)"""
                       rb"""|<meta[^>]*charset\s*=\s*["']?([\w.:-]+)""", re.IGNORECASE)
# HTML rules, keyed by the Python codec name (``codecs.lookup(declared).name``, so every alias
# counts): a declared Latin-1 is decoded as windows-1252; a UTF-16 declaration read in a
# byte-oriented document means UTF-8.
_HTML_CHARSETS = {"iso8859-1": "cp1252", "utf-16": "utf-8", "utf-16-le": "utf-8", "utf-16-be": "utf-8"}
# Python codecs that are not document charsets: a declaration naming one is ignored.
_NOT_CHARSETS = frozenset({"undefined", "idna", "punycode", "unicode-escape", "raw-unicode-escape",
                           "unicode_escape", "raw_unicode_escape", "utf-7"})


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
    target: str | None  # the in-document anchor the line links to, if any
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
        link = _ANCHOR_TARGET.search(raw)
        target, in_table = (link.group(1) if link else None), line.lstrip().startswith("|")
        part = _PART.match(text)
        if part:
            found.append(_Cand(offsets[i], end, "part", part.group(1).upper(), target, in_table))
            combined = _PART_THEN_ITEM.match(text)  # "PART II - ITEM 7. MD&A"
            text = text[combined.end():] if combined else ""
        item = _ITEM.match(text)
        if item:
            key = item.group(1).replace(".", "").upper()  # "Item 5.A" is Item 5A
            found.append(_Cand(offsets[i], end, "item", key, target, in_table))
    return found


def _position_index(rules: SectionRules) -> dict[tuple[str | None, str], int]:
    index: dict[tuple[str | None, str], int] = {}
    for i, (part, key, _) in enumerate(rules.positions):
        index.setdefault((part, key), i)
        index.setdefault((None, key), i)  # an item read without a Part: its first position
    return index


def _identities(cands: list[_Cand], rules: SectionRules, resets: set[int]) -> list[int | str | None]:
    """An item's identity: its position under the Part in effect (10-Q: Part I Item 3 and
    Part II Item 3 differ), else its key; a Part line has none. The Part in effect is reset
    after each index of ``resets`` (a Part read inside a dense run does not carry past it)."""
    index, family_parts = _position_index(rules), set(rules.parts)
    ident: list[int | str | None] = []
    part = None
    for n, c in enumerate(cands):
        if c.kind == "part":
            part = c.key if c.key in family_parts else None
            ident.append(None)
        else:
            pos = index.get((part, c.key), index.get((None, c.key)))
            ident.append(pos if pos is not None else c.key)
        if n in resets:
            part = None
    return ident


def _runs(markdown: str, cands: list[_Cand], rules: SectionRules,
          ident: list[int | str | None]) -> list[list[int]]:
    """Consecutive heading lines at most ``toc_max_gap_chars`` apart (or rows of one table),
    in order (see the module docstring)."""
    runs: list[list[int]] = []
    last_part = last_item = None
    for n, c in enumerate(cands):
        split = not runs or (not _same_table(markdown, cands[n - 1], c) and len(
            _norm(markdown[cands[n - 1].end:c.start])) > rules.toc_max_gap_chars)
        if c.kind == "part":
            order = _PART_ORDER.index(c.key)
            split = split or (last_part is not None and order <= last_part)
        else:
            order = ident[n] if isinstance(ident[n], int) else None
            split = split or (order is not None and last_item is not None and order <= last_item)
        if split:
            runs.append([])
            last_part = last_item = None
        runs[-1].append(n)
        if c.kind == "part":
            last_part = order
        elif order is not None:
            last_item = order
    return runs


def _same_table(markdown: str, a: _Cand, b: _Cand) -> bool:
    """Both lines are rows of one pipe table (no blank line between them)."""
    return a.in_table and b.in_table and "\n\n" not in markdown[a.end - 1:b.start]


def _tabular(markdown: str, cands: list[_Cand], run: list[int]) -> bool:
    """Most consecutive lines of the run are rows of one pipe table."""
    pairs = list(zip(run, run[1:]))
    same = sum(1 for a, b in pairs if _same_table(markdown, cands[a], cands[b]))
    return bool(pairs) and 2 * same >= len(pairs)


def _span(ident: list[int | str | None], skip: set[int]) -> tuple[dict, dict]:
    first: dict = {}
    last: dict = {}
    for n, i in enumerate(ident):
        if i is not None and n not in skip:
            first.setdefault(i, n)
            last[i] = n
    return first, last


def _classify(markdown: str, cands: list[_Cand], rules: SectionRules
              ) -> tuple[list[int | str | None], set[int], set[int]]:
    """``(identities, table-of-contents lines, ambiguous lines)``. Linear in the candidates."""
    ident = _identities(cands, rules, set())
    dense = [r for r in _runs(markdown, cands, rules, ident) if len(r) >= rules.toc_min_run]
    tabular = [_tabular(markdown, cands, run) for run in dense]
    ident = _identities(cands, rules, {
        run[-1] for run, table in zip(dense, tabular)
        if table or sum(cands[n].kind == "part" for n in run) >= rules.toc_min_part_lines})
    # 1. tables of item rows (a TOC or a cross-reference index, wherever it sits)
    toc = {n for run, table in zip(dense, tabular) if table for n in run}
    # 2. other dense runs repeated later by the rest of the document (a plain-line TOC)
    _, last = _span(ident, toc)
    undecided = []
    for run in dense:
        if run[0] in toc:
            continue
        items = {ident[n] for n in run if ident[n] is not None}
        repeated = sum(1 for i in items if last[i] > run[-1])
        if items and repeated >= rules.toc_min_later_share * len(items):
            toc.update(run)
        else:
            undecided.append(run)
    # 3. body: none of the run's items occurs anywhere else, and the document has an Item
    #    heading outside its dense runs (otherwise nothing says the run is not a TOC)
    first, last = _span(ident, toc)
    inside = {n for run in dense for n in run}
    corroborated = any(c.kind == "item" and n not in inside and not _toc_link(c, rules)
                       for n, c in enumerate(cands))
    ambiguous: set[int] = set()
    for run in undecided:
        if not corroborated or any(first[ident[n]] < run[0] or last[ident[n]] > run[-1]
                                   for n in run if ident[n] is not None):
            ambiguous.update(run)
    return ident, toc, ambiguous


def _toc_link(c: _Cand, rules: SectionRules) -> bool:
    """A line linking to an anchor inside a pipe table: a TOC entry, never a heading."""
    return rules.skip_table_anchor_links and c.target is not None and c.in_table


def _walk(cands: list[_Cand], idx: list[int], keys: list[str], lo: int, hi: int) -> list[tuple[int, int]]:
    """``(index in cands, index in keys)`` chosen for ``keys`` among ``cands[idx]`` within
    ``[lo, hi)``: walked from the last key, each the last occurrence before the next one chosen;
    a run of the same key with nothing between keeps its first occurrence."""
    inside = [n for n in idx if lo <= cands[n].start < hi]
    chosen, bound = [], hi
    for pos in range(len(keys) - 1, -1, -1):
        i = max((j for j, n in enumerate(inside) if cands[n].key == keys[pos] and cands[n].start < bound),
                default=None)
        if i is None:
            continue
        while i > 0 and cands[inside[i - 1]].key == keys[pos]:
            i -= 1
        bound = cands[inside[i]].start
        chosen.append((inside[i], pos))
    chosen.reverse()
    return chosen


# A boundary: (Markdown offset, order, section name or None). ``order`` is the item's index in
# ``rules.positions``; a Part heading sits half a step before its first item.
_Boundary = tuple[int, float, "str | None"]


def _heuristic(markdown: str, cands: list[_Cand], rules: SectionRules
               ) -> tuple[list[_Boundary] | None, list[_Cand]]:
    """``(boundaries chosen on the headings, Item heading lines)``; the boundaries are ``None``
    when one of them sits on an unexplained dense run."""
    size = len(markdown)
    _, toc, ambiguous = _classify(markdown, cands, rules)
    kept = [n for n, c in enumerate(cands) if n not in toc and not _toc_link(c, rules)]
    items = [n for n in kept if cands[n].kind == "item"]
    headings = [cands[n] for n in items]
    chosen: list[tuple[int, float]] = []  # (index in cands, order)
    if rules.segment_by_part:
        parts = [n for n in kept if cands[n].kind == "part" and cands[n].key in rules.parts]
        found = {rules.parts[pos]: n for n, pos in _walk(cands, parts, list(rules.parts), 0, size + 1)}
        for k, part in enumerate(rules.parts):
            own = [i for i, (p, _, _) in enumerate(rules.positions) if p == part]
            if part in found:
                lo = cands[found[part]].start
                chosen.append((found[part], (own[0] if own else len(rules.positions)) - 0.5))
            elif k == 0:
                lo = 0
            else:
                continue
            hi = min((cands[found[p]].start for p in rules.parts[k + 1:] if p in found), default=size + 1)
            chosen += [(n, own[pos]) for n, pos in
                       _walk(cands, items, [rules.positions[i][1] for i in own], lo, hi)]
    else:
        chosen += _walk(cands, items, [key for _, key, _ in rules.positions], 0, size + 1)
    if any(n in ambiguous for n, _ in chosen):
        return None, headings  # a heading on an unexplained dense run
    return [(cands[n].start, order, rules.positions[int(order)][2] if order == int(order) else None)
            for n, order in chosen], headings


def _anchored(cands: list[_Cand], rules: SectionRules, anchors: Mapping[str, int]
              ) -> tuple[list[_Boundary], int] | None:
    """``(boundaries, start of the last linked TOC line)`` from a linked table of contents:
    each listed item starts at its link target. ``None`` (use the heuristics) when no emitted
    item is linked forward, when a link does not resolve, when the targets are not in item
    order, or when a target is not after the last linked TOC line (a link into the TOC)."""
    entries: list[_Boundary] = []
    last_pos, part, toc_end = -1, None, -1
    for c in cands:
        if c.kind == "part":
            part = c.key
            continue
        if c.target is None:
            continue
        if c.target not in anchors:
            return None
        if anchors[c.target] <= c.start:  # a back-link (to the TOC) or a cross-reference
            continue
        pos = next((i for i in range(last_pos + 1, len(rules.positions))
                    if rules.positions[i][1] == c.key
                    and (part is None or rules.positions[i][0] in (None, part))), None)
        if pos is None:
            continue
        entries.append((anchors[c.target], pos, rules.positions[pos][2]))
        last_pos, toc_end = pos, c.start
    if (not any(name for _, _, name in entries) or entries[0][0] <= toc_end
            or any(b[0] <= a[0] for a, b in zip(entries, entries[1:]))):
        return None
    return entries, toc_end


def _merged(linked: list[_Boundary], toc_end: int, found: list[_Boundary] | None) -> list[_Boundary]:
    """The anchors, plus the heuristic boundaries of the items the TOC does not link, when
    the whole is consistent (after the TOC, in item order); else the anchors alone."""
    orders = {order for _, order, _ in linked}
    extra = [b for b in found or () if b[1] not in orders]
    both = sorted(linked + extra, key=lambda b: b[:2])
    if extra and all(b[0] > toc_end for b in extra) and all(
            a[0] < b[0] and a[1] < b[1] for a, b in zip(both, both[1:])):
        return both
    return linked


def _sections(boundaries: list[_Boundary], headings: list[_Cand], rules: SectionRules,
              size: int) -> tuple[Section, ...]:
    """Each named boundary runs to the next one. The last one runs to the end of the document,
    or to the first heading after it of another item of the form (an appendix repeating
    earlier items)."""
    boundaries = sorted(boundaries, key=lambda b: b[:2])
    known = {key for _, key, _ in rules.positions}
    sections = []
    for n, (start, order, name) in enumerate(boundaries):
        if name is None:
            continue
        if n + 1 < len(boundaries):
            end = boundaries[n + 1][0]
        else:
            own = rules.positions[int(order)][1]
            end = next((h.start for h in headings if h.start > start and h.key in known and h.key != own),
                       size)
        sections.append(Section(name=name, start=start, end=end))
    return tuple(sections)


def find_sections(markdown: str, form: str, *, rules: SectionRules | None = None,
                  anchors: Mapping[str, int] | None = None) -> tuple[Section, ...]:
    """Item sections of ``markdown`` under ``rules`` (default: the rules of ``form``'s family;
    a form without rules has no sections). ``anchors`` maps in-document anchor ids to Markdown
    offsets (as :func:`filing_document` computes them); a linked table of contents then gives
    exact section starts."""
    rules = rules if rules is not None else _rules_for(form)
    if rules is None:
        return ()
    cands = _scan(markdown, rules)
    boundaries, headings = _heuristic(markdown, cands, rules)
    linked = _anchored(cands, rules, anchors) if anchors else None
    if linked is not None:
        boundaries = _merged(*linked, boundaries)
    if not boundaries:
        return ()  # nothing found, or a heading on an unexplained run: no section rather than a wrong one
    return _sections(boundaries, headings, rules, len(markdown))


def _decode(html: bytes) -> str:
    m = _DECLARED.search(html[:4096])
    if m:
        try:
            info = codecs.lookup((m.group(1) or m.group(2)).decode("ascii").lower())
            if getattr(info, "_is_text_encoding", True) and info.name not in _NOT_CHARSETS:
                return html.decode(_HTML_CHARSETS.get(info.name, info.name))
        except (LookupError, UnicodeError):
            pass  # unknown codec name or wrong declaration: treated as undeclared
    try:
        return html.decode("utf-8")
    except UnicodeDecodeError:
        return html.decode("cp1252", errors="replace")


_VOID = frozenset({"br", "hr", "img", "input", "col", "area", "meta", "link", "wbr"})
_ROW_CONTAINERS = frozenset({"table", "thead", "tbody", "tfoot", "tr"})


def _mark_anchor(el, marker: str) -> None:
    """Put ``marker`` as text where the element starts, so it survives into the Markdown."""
    if el.tag in _VOID:
        el.tail = marker + (el.tail or "")
        return
    if el.tag in _ROW_CONTAINERS:
        cell = next(el.iter("td", "th"), None)
        el = cell if cell is not None else el
    el.text = marker + (el.text or "")


def _reduced_html(text: str) -> tuple[str, str, list[str]]:
    """Hidden content removed, link targets marked, inline wrappers unwrapped, presentation
    attributes dropped (lxml: a native parse, much cheaper than a BeautifulSoup tree on
    multi-MB filings). Returns ``(html, marker, anchor ids)``: anchor ``n`` is marked by
    ``f"{marker}{n}{marker}"``."""
    parser = lxml.html.HTMLParser(encoding="utf-8", huge_tree=True, remove_comments=True)
    root = lxml.html.document_fromstring(text.encode("utf-8"), parser=parser)
    dropped = [el for el in root.iter() if isinstance(el.tag, str) and (
        el.tag in ("script", "style", "ix:header") or "hidden" in el.attrib
        or _HIDDEN_STYLE.search(el.get("style") or ""))]
    for el in dropped:
        if el.getparent() is not None:
            el.drop_tree()  # keeps the element's tail text
    marker = "zqanchorq"
    while marker in text:
        marker += "q"
    targets = {(a.get("href") or "")[1:] for a in root.iter("a") if (a.get("href") or "").startswith("#")}
    ids: list[str] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        name = el.get("id") or (el.get("name") if el.tag == "a" else None)
        if name and name in targets:
            targets.discard(name)  # the first element with that id
            _mark_anchor(el, f"{marker}{len(ids)}{marker}")
            ids.append(name)
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if (el.tag in _UNWRAPPED_TAGS or el.tag.startswith("ix:")) and el.getparent() is not None:
            el.drop_tag()  # keeps its text and children
            continue
        for name in [k for k in el.attrib if k not in _KEPT_ATTRIBUTES]:
            del el.attrib[name]
        if "start" in el.attrib and not _INTEGER.fullmatch(el.attrib["start"].strip()):
            del el.attrib["start"]
    return lxml.html.tostring(root, encoding="unicode"), marker, ids


def _unmark(markdown: str, marker: str, ids: list[str]) -> tuple[str, dict[str, int]]:
    """Markers removed; each anchor at the start of the first non-blank line from its marker.
    A paragraph holding only markers (an empty element, which an unmarked document does not
    render at all) goes whole, with the blank lines after it."""
    one = re.escape(marker) + r"(\d+)" + re.escape(marker)
    pattern = re.compile(rf"(?P<line>(?:(?<=\n\n)|\A)[ \t]*(?:{one}[ \t]*)+(?:\n\n+|\Z))|{one}")
    number = re.compile(one)
    pieces, where, last, length = [], {}, 0, 0
    for m in pattern.finditer(markdown):
        pieces.append(markdown[last:m.start()])
        length += m.start() - last
        for n in number.finditer(m.group(0)):
            where[int(n.group(1))] = length
        last = m.end()
        if m.group("line") is not None and last == len(markdown):
            pieces = ["".join(pieces).rstrip()]  # a marker-only last line: no trailing blank lines
    pieces.append(markdown[last:])
    clean = "".join(pieces)
    anchors = {}
    for n, at in where.items():
        if n < len(ids):
            text = re.compile(r"\S").search(clean, min(at, len(clean)))
            at = text.start() if text else len(clean)
            anchors[ids[n]] = clean.rfind("\n", 0, at) + 1
    return clean, anchors


def _convert(html: bytes, max_html_bytes: int) -> tuple[str, dict[str, int]]:
    if len(html) > max_html_bytes:
        raise DocumentTooLarge(f"{len(html)} bytes > max_html_bytes={max_html_bytes}")
    text = _decode(html)
    if not text.strip():
        return "", {}
    try:
        reduced, marker, ids = _reduced_html(text)
    except lxml.etree.ParserError:  # nothing parseable (e.g. only a comment)
        return "", {}
    return _unmark(HtmlConverter().convert_string(reduced).markdown, marker, ids)


def html_to_markdown(html: bytes, *, max_html_bytes: int = MAX_HTML_BYTES) -> str:
    """EDGAR HTML -> Markdown, without hidden content (see the module docstring)."""
    return _convert(html, max_html_bytes)[0]


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
    markdown, anchors = _convert(html, max_html_bytes)
    return FilingDocument(ref=ref, markdown=markdown,
                          sections=find_sections(markdown, ref.form, anchors=anchors))
