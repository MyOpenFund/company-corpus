"""Euronext backend — one site, many markets (NL/BE/FR/PT/NO/IE).

``live.euronext.com`` is a single platform fronting every Euronext market, so a
single backend covers them all: the per-issuer **notices** feed is keyed by
``(ISIN, MIC)`` and the MIC follows from the entity's country.

* **Feed:** ``GET /en/ajax/getNoticePublicData/<ISIN>-<MIC>`` → an HTML table of
  the issuer's exchange notices (corporate events: dividends, admissions, name
  changes, general-meeting notices…), each with a date, a type and — when an
  attachment exists — a ``notice-download`` PDF link.
* **Identity:** ISIN-keyed (no-guess). The entity's GLEIF ISINs drive the query;
  notices are de-duplicated by their Euronext notice id across ISINs.

This is a **complement** to the national OAMs (AFM, FSMA, AMF, Oslo Børs…), which
are more complete for financial reports / ad-hoc: Euronext adds the exchange's
corporate-event notices.  It is the primary EU source only where no national
backend exists (Portugal; Ireland once its route is wired).  The acquisition
dispatcher lists it *after* the national backend, so any genuine overlap keeps
the national document (first-wins dedup in ``merge_documents``).
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

# ---------------------------------------------------------------------------
# Country -> Euronext market MIC
# ---------------------------------------------------------------------------

# Ireland is intentionally absent: Euronext Dublin's per-issuer notices feed is
# empty (verified live), and Irish issuers' regulated information is held by the
# FCA NSM — so they resolve through the GB (NsmGB) backend by LEI instead.
EURONEXT_MICS: dict[str, str] = {
    "NL": "XAMS",
    "BE": "XBRU",
    "FR": "XPAR",
    "PT": "XLIS",
    "NO": "XOSL",
}

# Any MIC works for a listing lookup — the notices feed keys on the ISIN and
# ignores the MIC in the URL (verified: ASML's ISIN under XOSL returns ASML's
# Amsterdam notices). Used by the listing fallback for non-Euronext-home issuers.
_LISTING_MIC = "XPAR"

_FEED_URL = "https://live.euronext.com/en/ajax/getNoticePublicData/"
_DOWNLOAD_BASE = "https://live.euronext.com"
# The feed paginates via ``?pageSize=50&alias=1&pageNum=N`` (the pager's own
# params — ``page``/``items_per_page`` are ignored, and ``pageSize`` is capped at
# 50 server-side, so only ``pageNum`` advances). Page through to exhaustivity.
_PAGE_SIZE = 50
_MAX_PAGES = 200  # 10k notices/issuer — a backstop, recorded if ever hit.

# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------

# A row STARTS here; each row is sliced from its start to the next row start (or
# end), so nested markup inside a cell (buttons, expanded abstracts, even a
# nested table) cannot truncate it at the first inner </tr> — that would silently
# drop the trailing download cell.
_ROW_START_RE = re.compile(r'<tr\b[^>]*\bclass="row_(\d+)\b', re.I)
_NOTICENUMBER_RE = re.compile(r'<td[^>]*\bclass="noticenumber"[^>]*>\s*([^<]+?)\s*</td>', re.I)
_NOTICEDATE_RE = re.compile(r'<td[^>]*\bclass="noticedate[^"]*"[^>]*>\s*([^<]+?)\s*</td>', re.I)
_NOTICENAME_TD_RE = re.compile(r'<td[^>]*\bclass="noticename[^"]*"[\s\S]*?</td>', re.I)
# The 'instruments' cell carries the issuer name — used to verify a notice really
# belongs to the entity when querying by listing (the feed is ISIN-keyed but can
# return market-wide "Multiple" notices for an ISIN it does not list).
_INSTRUMENTS_TD_RE = re.compile(r'<td[^>]*\bclass="instruments[^"]*"[\s\S]*?</td>', re.I)
_NAME_NORM_RE = re.compile(r'[^a-z0-9]+')
# Capture the whole notice-download query string; params are parsed order-free
# below (Euronext does not guarantee a fixed param order).
_DOWNLOAD_HREF_RE = re.compile(r'notice-download\?([^"\'<>\s]+)', re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# The notice-name / instruments cells embed a collapse button whose screen-reader
# label ("Toggle Visibility") would otherwise pollute the extracted text.
_SR_ONLY_RE = re.compile(r'<span\b[^>]*\bclass="sr-only"[^>]*>.*?</span>', re.I | re.S)

# ---------------------------------------------------------------------------
# doc_type mapping (Euronext notices are mostly corporate events)
# ---------------------------------------------------------------------------

_TYPE_RULES: list[tuple[str, str]] = [
    ("annual financial report", "annual_report"),
    ("annual report", "annual_report"),
    ("half-year", "half_year_report"),
    ("half year", "half_year_report"),
    ("interim", "interim_statement"),
    ("prospectus", "prospectus"),
    ("general meeting", "governance"),
    ("meeting of", "governance"),
    ("change of issuer", "governance"),
    ("change of product name", "governance"),
    ("voting rights", "holding_notification"),
]


def _doc_type(notice_name: str) -> str:
    """Map a Euronext notice type to a ``DOC_TYPES`` member (default ``other``)."""
    t = (notice_name or "").lower()
    for keyword, mapped in _TYPE_RULES:
        if keyword in t:
            return mapped
    return "other"


def _published_ts(text: str) -> str | None:
    """Convert a Euronext notice date (``23 Apr 2026``) to an ISO-8601 string."""
    try:
        d = datetime.strptime(text.strip(), "%d %b %Y").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return d.isoformat()


def _cell_text(html_fragment: str) -> str:
    """Strip tags (and the collapse button's sr-only label) and collapse whitespace."""
    cleaned = _SR_ONLY_RE.sub(" ", html_fragment)
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", cleaned)).strip()


def _norm(s: str) -> str:
    """Lower-case, drop non-alphanumerics, collapse — for issuer-name matching."""
    return _NAME_NORM_RE.sub(" ", (s or "").lower()).strip()


def _instrument_matches(row: str, want_norm: str) -> bool:
    """True when a notice row's issuer cell matches the entity (suffix-stripped
    containment), rejecting the feed's market-wide ``Multiple`` notices."""
    m = _INSTRUMENTS_TD_RE.search(row)
    instr = _norm(_cell_text(m.group(0))) if m else ""
    if not instr or instr == "multiple" or not want_norm:
        return False
    return instr in want_norm or want_norm in instr


class EuronextSource(OamSource):
    """Euronext exchange-notices backend, shared across all Euronext markets.

    The market MIC is derived from the entity's country via :data:`EURONEXT_MICS`;
    an entity in a non-Euronext country yields nothing.
    """

    name = "euronext"
    country = "EU"  # multi-market; each Document carries the entity's own country.

    def __init__(self, fetcher=None, config=None, *, force_mic: str | None = None):
        """``force_mic`` enables *listing* mode: the entity's home country has no
        backend, but it may be LISTED on a Euronext venue. The notices feed is
        ISIN-keyed (the MIC in the URL is ignored — any value works), so we query
        by the entity's ISINs and **verify each notice's issuer name** matches the
        entity, rejecting the market-wide "Multiple" notices the feed returns for
        ISINs it does not actually list (no-guess)."""
        super().__init__(fetcher, config)
        self._force_mic = force_mic

    def discover(self, entity: Entity) -> list[Document]:
        """Return the issuer's Euronext notices.

        Home-market mode: the MIC is the entity-country's Euronext venue. Listing
        mode (``force_mic``): query by ISIN regardless of home country, keeping
        only notices whose issuer name matches the entity. De-duplicates by notice
        id. An entity outside Euronext (home mode, no venue) returns ``[]``.
        """
        listing = self._force_mic is not None
        mic = self._force_mic or EURONEXT_MICS.get(entity.country)
        if not mic:
            return []
        isins = [i for i in (entity.isins or ()) if i]
        if not isins:
            self._record_error(
                "no-isin",
                _FEED_URL,
                RuntimeError(
                    f"entity {entity.name!r} has no ISIN; the Euronext notices feed "
                    "is ISIN-keyed and cannot be queried without one"
                ),
            )
            return []

        now = datetime.now(timezone.utc).isoformat()
        want = _norm(entity.name)
        seen: set[str] = set()
        docs: list[Document] = []
        for isin in isins:
            for notice_id, row in self._fetch_notices(isin, mic):
                if notice_id in seen:
                    continue
                seen.add(notice_id)
                if listing and not _instrument_matches(row, want):
                    continue  # market-wide noise / wrong issuer — never bind
                docs.append(self._to_document(notice_id, row, entity, mic, now))
        return docs

    def _fetch_notices(self, isin: str, mic: str):
        """Yield ``(notice_id, row_html)`` across all pages for one ``ISIN-MIC``.

        Pages via the feed's own ``pageNum`` param until a short or empty page is
        returned (the last page), recording truncation only if the page backstop
        is hit (never a silent cut-off).
        """
        base = f"{_FEED_URL}{isin}-{mic}"
        for page in range(1, _MAX_PAGES + 1):
            url = f"{base}?pageSize={_PAGE_SIZE}&alias=1&pageNum={page}"
            try:
                html = self.fetcher.get_text(url)
            except Exception as exc:  # noqa: BLE001
                self._record_error("notices", url, exc)
                return
            rows = list(self._rows(html))
            yield from rows
            if len(rows) < _PAGE_SIZE:
                return  # short/empty page = the last one
        self._record_error(
            "truncated",
            base,
            RuntimeError(f"{isin}-{mic}: hit the {_MAX_PAGES}-page cap; older notices not fetched"),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _rows(html: str):
        """Yield ``(notice_id, row_html)`` for each notice row in the feed.

        Each row is sliced from its ``<tr class="row_…">`` start to the next
        row's start (or the document end), so nested markup in a cell cannot
        truncate the row at an inner ``</tr>`` and silently drop its tail.
        """
        starts = [(m.start(), m.group(1)) for m in _ROW_START_RE.finditer(html)]
        for i, (pos, notice_id) in enumerate(starts):
            end = starts[i + 1][0] if i + 1 < len(starts) else len(html)
            yield notice_id, html[pos:end]

    def _to_document(
        self, notice_id: str, row: str, entity: Entity, mic: str, now: str
    ) -> Document:
        """Build a :class:`Document` from one notice row.

        A row with a PDF attachment gets a downloadable file; one without is kept
        as an index-only record (its metadata, no file) — never silently dropped.
        """
        row_u = _html.unescape(row)
        num_m = _NOTICENUMBER_RE.search(row)
        date_m = _NOTICEDATE_RE.search(row)
        name_td = _NOTICENAME_TD_RE.search(row)
        notice_name = _cell_text(name_td.group(0)) if name_td else ""
        notice_number = num_m.group(1).strip() if num_m else notice_id

        files: list[dict] = []
        href = _DOWNLOAD_HREF_RE.search(row_u)
        if href:
            q = parse_qs(href.group(1))
            did = (q.get("id") or [""])[0]
            aid = (q.get("attachmentId") or [""])[0]
            ftype = (q.get("type") or [""])[0].upper()
            if did and aid:
                # Build the canonical PDF download URL (params order-independent).
                files.append({
                    "name": f"euronext-{notice_number}.pdf",
                    "kind": "document",
                    "url": f"{_DOWNLOAD_BASE}/en/listview/notice-download"
                           f"?id={did}&type={ftype or 'PDF'}&attachmentId={aid}",
                })
            else:
                # A download link is present but its ids could not be extracted —
                # surface the parser drift instead of silently losing the file.
                self._record_error(
                    "download-parse",
                    f"{_DOWNLOAD_BASE}/en/listview/notice-download?{href.group(1)}",
                    RuntimeError(f"notice {notice_number}: unparseable download link"),
                )

        return Document(
            doc_id=f"euronext-{notice_id}",
            lei=entity.lei,
            country=entity.country,
            doc_type=_doc_type(notice_name),
            period_end=None,
            published_ts=_published_ts(date_m.group(1)) if date_m else None,
            discovered_ts=now,
            language="en",
            source=self.name,
            files=files,
            native_meta={
                "title": notice_name,
                "notice_number": notice_number,
                "mic": mic,
                "has_attachment": bool(files),
            },
        )
