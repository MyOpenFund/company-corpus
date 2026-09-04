"""Finanscentralen (Finansinspektionen) OAM backend — Sweden.

The Swedish OAM is the Finanscentralen register at finanscentralen.fi.se. The
site is an ASP.NET WebForms application with no public API. The flow is:

1. **Session bootstrap:** GET ``/search/search.aspx`` to obtain the
   ``ASP.NET_SessionId`` cookie and scrape the WebForms hidden fields
   (``__VIEWSTATE``, ``__VIEWSTATEGENERATOR``, ``__EVENTVALIDATION``).

2. **Name-based search:** POST back to ``search.aspx`` with the hidden fields
   plus ``ctl00$main$txtCompanyName=<name>``.  On a successful match the server
   returns the issuer's company-profile page directly (no redirect to a list).
   The profile page has ``<form action="ViewCompany2.aspx">``.

3. **Parse six GridView tables** from the profile page.

4. **Download:** ``GET /search/GetFile.aspx?fid=<N>`` — stateless.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

_BASE = "https://finanscentralen.fi.se"
_SEARCH_URL = _BASE + "/search/search.aspx"
_GETFILE_BASE = _BASE + "/search/GetFile.aspx?fid="
# gvStockAffect rows have no GetFile link; their downloadable item is the
# ViewStockAffect popup which 302-redirects to GetFile (download.py follows it).
_VIEWSTOCK_BASE = _BASE + "/search/ViewStockAffect.aspx?id="

_VIEWCOMPANY_URL = _BASE + "/search/ViewCompany2.aspx"

_BTN_SEARCH = "ctl00$main$btnSearch"
_BTN_VALUE = "Sök"

# Each GridView shows 10 rows/page. Cap paging per grid so a prolific issuer cannot
# loop forever; record a truncation past the cap so the incompleteness is visible.
_MAX_PAGES_PER_GRID = 30

_GRIDS: list[tuple[str, str]] = [
    ("gvwYearReports",     "annual_report"),
    ("gvwHalfYearReports", "half_year_report"),
    ("gvwQuarterReports",  "interim_statement"),
    ("gvwBookEndReports",  "other"),
    ("gvwFlaggings",       "holding_notification"),
    ("gvStockAffect",      "other"),
]

_HIDDEN_TAG_RE = re.compile(r'<input\b([^>]*/?>)', re.I | re.S)
_HIDDEN_VALUE_RE = re.compile(r'\bvalue="([^"]*)"', re.I)
_HIDDEN_NAME_ATTR_RE = re.compile(r'\bname="([^"]*)"', re.I)
_PROFILE_RE = re.compile(r'action="ViewCompany2\.aspx"', re.I)
_COMPANY_NAME_RE = re.compile(
    r'<span\b[^>]*\bid="ctl00_main_lblCompanyName"[^>]*>([^<]*)</span>', re.I
)
_TABLE_RE_TMPL = r'(<table\b[^>]*\bid="ctl00_main_{suffix}"[^>]*>.*?</table>)'
_ROW_RE = re.compile(r'<tr\b[^>]*>.*?</tr>', re.S | re.I)
_FID_RE = re.compile(r"<a\b[^>]*href='/search/GetFile\.aspx\?fid=(\d+)'[^>]*>([^<]*)</a>", re.I)
_FLAGGING_ID_RE = re.compile(r"EditFlagging\.aspx\?id=(\d+)", re.I)
_STOCKAFFECT_ID_RE = re.compile(r"ViewStockAffect\.aspx\?id=(\d+)", re.I)
# A grid advertises a further page when its pager exposes a Page$Next postback.
_PAGE_NEXT_RE = re.compile(r"Page\$Next", re.I)
_HEADLINE_RE = re.compile(r'<span\b[^>]*title="([^"]*)"', re.I)
_PERIOD_RE = re.compile(r'^\s*(20\d\d(?:-\d{2}(?:-\d{2})?)?)\b', re.M)
_DATE_PREFIX_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')
_TAG_STRIP_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')
_LEGAL_SUFFIX_RE = re.compile(r',?\s*(?:aktiebolag|ab\s+publ\.?|ab\b)$', re.I)


def _stockaffect_doc_type(category: str) -> str:
    """Map a gvStockAffect 'Kategori' text to a DOC_TYPES member.

    The regulated-announcement grid mixes inside information (MAR Art. 17) with
    home-member-state notices, rights changes, buybacks, etc. Only the
    ``Insiderinformation`` category is inside_information; everything else is
    ``other`` (still a valid DOC_TYPES member).
    """
    if 'insiderinformation' in (category or '').casefold():
        return 'inside_information'
    return 'other'


def _normalise(name: str) -> str:
    """Collapse whitespace, casefold, strip diacritics and Swedish legal suffix."""
    n = _WS_RE.sub(' ', name).strip().casefold()
    n = ''.join(c for c in unicodedata.normalize('NFKD', n) if not unicodedata.combining(c))
    n = _LEGAL_SUFFIX_RE.sub('', n).strip()
    return n


def _scrape_hidden(html: str, field: str) -> str:
    """Return the value= of a WebForms hidden <input name=FIELD …>."""
    for m in _HIDDEN_TAG_RE.finditer(html):
        tag_text = m.group(1)
        nm = _HIDDEN_NAME_ATTR_RE.search(tag_text)
        if nm and nm.group(1) == field:
            vm = _HIDDEN_VALUE_RE.search(tag_text)
            return vm.group(1) if vm else ''
    return ''


def _strip_tags(html: str) -> str:
    return _WS_RE.sub(' ', _TAG_STRIP_RE.sub(' ', html)).strip()


def _period_from_cell(cell_html: str) -> str | None:
    text = _strip_tags(cell_html)
    m = _PERIOD_RE.search(text)
    return m.group(1).strip() if m else None


def _published_from_cell(cell_html: str) -> str | None:
    text = _strip_tags(cell_html)
    m = _DATE_PREFIX_RE.search(text)
    return m.group(1) if m else None


class OamSE(OamSource):
    """Sweden OAM backend — Finanscentralen ASP.NET WebForms scraper."""

    name = "oam-se"
    country = "SE"

    def discover(self, entity: Entity) -> list[Document]:
        if not entity.name:
            return []

        # 1. Bootstrap: GET the search page.
        try:
            search_html = self.fetcher.get_text(_SEARCH_URL)
        except Exception as exc:  # noqa: BLE001
            self._record_error('bootstrap', _SEARCH_URL, exc)
            return []

        vs = _scrape_hidden(search_html, '__VIEWSTATE')
        ev = _scrape_hidden(search_html, '__EVENTVALIDATION')
        vsg = _scrape_hidden(search_html, '__VIEWSTATEGENERATOR')

        # 2. POST the name search.
        post_body = {
            '__VIEWSTATE': vs,
            '__VIEWSTATEGENERATOR': vsg,
            '__EVENTVALIDATION': ev,
            '__VIEWSTATEENCRYPTED': '',
            '__EVENTTARGET': '',
            '__EVENTARGUMENT': '',
            '__SEARCH_UTIL_CULTURE': 'sv-SE',
            '__SEARCH_UTIL_STARTPAGE': '',
            '__SEARCH_UTIL_SEARCHTEXT': '',
            'ctl00$main$txtCompanyName': entity.name,
            'ctl00$main$txtOrganizationNumber': '',
            'ctl00$main$txtOrganizationShortName': '',
            _BTN_SEARCH: _BTN_VALUE,
        }
        try:
            profile_html = self.fetcher.post_text(_SEARCH_URL, post_body)
        except Exception as exc:  # noqa: BLE001
            self._record_error('search', _SEARCH_URL, exc)
            return []

        # 3. Verify we got a company-profile page.
        if not _PROFILE_RE.search(profile_html):
            self._record_error(
                'search',
                _SEARCH_URL,
                RuntimeError(
                    f'search for {entity.name!r} did not return a company profile page '
                    '(ViewCompany2.aspx form action not found)'
                ),
            )
            return []

        # 3b. No-guess identity: the search does a *substring* match and, when a
        # single company matches, jumps straight to its profile — even if it is
        # the wrong subsidiary (e.g. "Nordea" -> "Nordea Hypotek Aktiebolag").
        # GLEIF hands us the full Swedish legal name (e.g. "ATLAS COPCO
        # AKTIEBOLAG"), so require the profile's own name to be *equal* to the
        # entity once both are suffix-stripped and normalised. Equality (not
        # containment) is what rejects a prefix match to a different company.
        name_m = _COMPANY_NAME_RE.search(profile_html)
        profile_name = name_m.group(1).strip() if name_m else ''
        want, got = _normalise(entity.name), _normalise(profile_name)
        if not got or want != got:
            self._record_error(
                'name-mismatch',
                _SEARCH_URL,
                RuntimeError(
                    f'search for {entity.name!r} returned profile {profile_name!r} '
                    '(name does not match — refusing to bind to a different company)'
                ),
            )
            return []

        # 4. Parse the six GridView tables (paging each via ViewCompany2.aspx).
        now = datetime.now(timezone.utc).isoformat()
        out: list[Document] = []
        for grid_suffix, grid_doc_type in _GRIDS:
            try:
                out.extend(
                    self._discover_grid(
                        grid_suffix, grid_doc_type, profile_html, entity, now
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self._record_error(f'grid-{grid_suffix}', _VIEWCOMPANY_URL, exc)
        return out

    # ------------------------------------------------------------------
    # Per-grid discovery + pagination
    # ------------------------------------------------------------------

    def _discover_grid(
        self,
        grid_suffix: str,
        grid_doc_type: str,
        first_page_html: str,
        entity: Entity,
        now: str,
    ) -> list[Document]:
        """Parse one GridView across all its pages.

        Each page after the first is fetched by POSTing ``Page$Next`` to
        ViewCompany2.aspx, threading the FRESHEST ``__VIEWSTATE`` /
        ``__EVENTVALIDATION`` through every POST (ASP.NET rejects a stale carousel).
        """
        out: list[Document] = []
        current_html = first_page_html

        for page in range(_MAX_PAGES_PER_GRID):
            table_html = _grid_table_html(current_html, grid_suffix)
            for row_data in _rows_from_table(table_html, grid_suffix):
                doc = self._row_to_doc(grid_suffix, grid_doc_type, row_data, entity, now)
                if doc is not None:
                    out.append(doc)

            if not table_html or not _PAGE_NEXT_RE.search(table_html):
                break  # no further page for this grid

            if page + 1 >= _MAX_PAGES_PER_GRID:
                self._record_error(
                    'truncated',
                    _VIEWCOMPANY_URL,
                    RuntimeError(
                        f'grid {grid_suffix} exceeded the {_MAX_PAGES_PER_GRID}-page '
                        'cap; remaining pages not crawled'
                    ),
                )
                break

            page_data = {
                '__VIEWSTATE': _scrape_hidden(current_html, '__VIEWSTATE'),
                '__VIEWSTATEGENERATOR': _scrape_hidden(current_html, '__VIEWSTATEGENERATOR'),
                '__EVENTVALIDATION': _scrape_hidden(current_html, '__EVENTVALIDATION'),
                '__VIEWSTATEENCRYPTED': _scrape_hidden(current_html, '__VIEWSTATEENCRYPTED'),
                '__EVENTTARGET': f'ctl00$main${grid_suffix}',
                '__EVENTARGUMENT': 'Page$Next',
            }
            try:
                current_html = self.fetcher.post_text(_VIEWCOMPANY_URL, page_data)
            except Exception as exc:  # noqa: BLE001
                self._record_error(f'page-{grid_suffix}', _VIEWCOMPANY_URL, exc)
                break

        return out

    def _row_to_doc(self, grid_suffix, grid_doc_type, row_data, entity, now):
        """Build a Document from a row, dispatching gvStockAffect's per-row doc_type."""
        if grid_suffix == 'gvStockAffect':
            doc_type = _stockaffect_doc_type(row_data.get('category') or '')
        else:
            doc_type = grid_doc_type
        return _row_to_document(row_data, doc_type, grid_suffix, entity, now, self.name)


def _grid_table_html(html: str, grid_suffix: str) -> str:
    """Return the <table>…</table> HTML for a grid, or '' if absent."""
    pattern = re.compile(
        _TABLE_RE_TMPL.format(suffix=re.escape(grid_suffix)),
        re.S | re.I,
    )
    m = pattern.search(html)
    return m.group(1) if m else ''


def _rows_from_table(table_html: str, grid_suffix: str) -> list[dict]:
    """Parse the data rows (skipping the header) of one grid table."""
    if not table_html:
        return []
    rows = _ROW_RE.findall(table_html)
    if not rows:
        return []
    return [_parse_row(row_html, grid_suffix) for row_html in rows[1:]]


def _parse_row(row_html: str, grid_suffix: str) -> dict:
    """Parse one <tr> into a row-dict."""
    tds = re.findall(r'<td\b[^>]*>(.*?)</td>', row_html, re.S | re.I)

    period = _period_from_cell(tds[0]) if tds else None

    files: list[dict] = []
    for fid, lang_label in _FID_RE.findall(row_html):
        lang = _lang_to_code(lang_label.strip())
        files.append({'fid': fid, 'url': _GETFILE_BASE + fid, 'language': lang})

    flagging_m = _FLAGGING_ID_RE.search(row_html)
    flagging_id = flagging_m.group(1) if flagging_m else None

    stockaffect_m = _STOCKAFFECT_ID_RE.search(row_html)
    stockaffect_id = stockaffect_m.group(1) if stockaffect_m else None

    headline_m = _HEADLINE_RE.search(row_html)
    headline = headline_m.group(1) if headline_m else None

    category = _strip_tags(tds[1]) if len(tds) > 1 and grid_suffix == 'gvStockAffect' else None

    published_ts = None
    if grid_suffix in ('gvwFlaggings', 'gvStockAffect') and tds:
        published_ts = _published_from_cell(tds[0])

    return {
        'period': period,
        'files': files,
        'flagging_id': flagging_id,
        'stockaffect_id': stockaffect_id,
        'headline': headline,
        'category': category,
        'published_ts': published_ts,
    }


def _lang_to_code(label: str) -> str | None:
    lower = label.casefold()
    if 'engelsk' in lower or 'english' in lower:
        return 'en'
    if 'svensk' in lower or 'swedish' in lower:
        return 'sv'
    return None


def _row_to_document(
    row_data: dict,
    doc_type: str,
    grid_suffix: str,
    entity: Entity,
    now: str,
    source_name: str,
) -> Document | None:
    """Build a Document from a parsed row-dict. Returns None for empty rows."""
    period = row_data.get('period')
    files = row_data.get('files') or []
    flagging_id = row_data.get('flagging_id')
    stockaffect_id = row_data.get('stockaffect_id')
    headline = row_data.get('headline')
    published_ts = row_data.get('published_ts')

    if not period and not flagging_id and not stockaffect_id:
        return None

    file_entries: list[dict] = [
        {
            'url': f['url'],
            'kind': 'document',
            'language': f.get('language'),
            'fid': f['fid'],
        }
        for f in files
    ]

    # gvStockAffect rows expose no GetFile link, but the ViewStockAffect popup
    # 302-redirects to the underlying PDF, so emit it as a downloadable file.
    # Flaggings remain index-only (no file).
    if stockaffect_id:
        file_entries.append({
            'url': _VIEWSTOCK_BASE + stockaffect_id,
            'kind': 'document',
            'language': None,
        })

    if flagging_id:
        doc_id = f'se-flagging-{flagging_id}'
    elif stockaffect_id:
        doc_id = f'se-stockaffect-{stockaffect_id}'
    elif period and files:
        fids_str = '-'.join(f['fid'] for f in files)
        doc_id = f'se-{grid_suffix.lower()}-{period}-{fids_str}'
    elif period:
        doc_id = f'se-{grid_suffix.lower()}-{period}-nofid'
    else:
        return None

    return Document(
        doc_id=doc_id,
        lei=entity.lei,
        country='SE',
        doc_type=doc_type,
        period_end=None,
        published_ts=published_ts,
        discovered_ts=now,
        language=None,
        source=source_name,
        files=file_entries,
        native_meta={
            'grid': grid_suffix,
            'period': period,
            'flagging_id': flagging_id,
            'stockaffect_id': stockaffect_id,
            'headline': headline,
        },
    )
