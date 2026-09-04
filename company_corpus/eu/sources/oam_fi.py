"""Nasdaq Helsinki OAM (oam.fi) backend — Finland.

Flow per entity:
1. **Bootstrap (once per instance):** GET https://oam.fi/ → scrape CSRF token
   (<meta name="_csrf" content="…">) and the embedded company list (name→integer OAM id).
2. **Resolve name → OAM company id:** exact normalised match against the company list
   (collapse whitespace, casefold, strip diacritics + trailing legal suffixes OYJ/OY/ABP/PLC).
   Strict: 0 or >1 matches → _record_error + return [].
3. **Paginated search:** POST https://oam.fi/ urlencoded with CSRF + company id →
   server-rendered HTML result rows. Paginate via `page` param until totalDataLength
   consumed (cap _MAX_PAGES=60). Pages are 1-indexed.
4. **Detail hop:** GET https://oam.fi/view/{view_id}?lang=en → parse attachment links
   (<nef-link class="attachment-link" href="/cns-web/oam/viewAttachment.action?messageAttachmentId={att_id}">).
5. **Emit Document** per view_id: doc_id=f"fi-{view_id}", files pointing at
   viewAttachment.action URLs.

Every step wrapped; one failure recorded, never aborts the rest.
"""
from __future__ import annotations

import html
import json
import re
import unicodedata
from datetime import datetime, timezone

from ..documents import DOC_TYPES, Document
from ..entities import Entity
from ..oam_base import IssuerRef, OamSource

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE = "https://oam.fi"
_SEARCH_URL = "https://oam.fi/"
_VIEW_URL = "https://oam.fi/view/{view_id}?lang=en"
_ATTACHMENT_URL = "https://oam.fi/cns-web/oam/viewAttachment.action?messageAttachmentId={att_id}"

_MAX_PAGES = 60
_PAGE_SIZE = 50

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# CSRF token: <meta name="_csrf" content="TOKEN">
_CSRF_RE = re.compile(r'<meta[^>]+name="_csrf"[^>]+content="([^"]+)"', re.I)

# Company select: extract the options="..." attribute from the company-select element.
# The fixture uses id="company-select" and name="company".
# We extract the full tag first, then pull the options attribute value out.
_COMPANY_SELECT_TAG_RE = re.compile(
    r'<nef-form-select\b[^>]*\bid="company-select"[^>]*>',
    re.S | re.I,
)

# Extract the options="..." attribute value from a nef-form-select tag.
_OPTIONS_ATTR_RE = re.compile(r'\boptions="([^"]*)"', re.S)

# A single result row block.
_ROW_BLOCK_RE = re.compile(
    r'<nef-table-row\b[^>]*\bclass="message-row"[^>]*>.*?</nef-table-row>',
    re.S,
)

# Result row date: the title attribute of the span inside table-published cell.
# <nef-table-cell class="table-published">...<span class="table-content" title="2025-10-28 17:30:00 EET" ...>
_ROW_DATE_RE = re.compile(
    r'<nef-table-cell[^>]+table-published[^>]*>.*?'
    r'<span[^>]+title="([^"]+)"',
    re.S,
)

# Result row headline + view_id:
# <nef-link href="/view/{view_id}?lang=en" ...><span class="table-link">TITLE</span>
_ROW_LINK_RE = re.compile(
    r'<nef-link[^>]+href="/view/(\d+)\?lang=en"[^>]*>'
    r'.*?<span class="table-link">([^<]+)</span>',
    re.S,
)

# Result row category: nef-table-cell.table-category → .table-content
_ROW_CATEGORY_RE = re.compile(
    r'<nef-table-cell[^>]+table-category[^>]*>.*?'
    r'<span class="table-content">([^<]+)</span>',
    re.S,
)

# Pagination total: totalDataLength="N"
_PAGINATION_RE = re.compile(r'totalDataLength="(\d+)"', re.I)

# Attachment link in view page. The tag spans multiple lines with attributes in any order.
# We match <nef-link with class="attachment-link" and href containing messageAttachmentId=N
# then capture the filename text content.
_ATTACHMENT_RE = re.compile(
    r'<nef-link\b[^>]*class="attachment-link"[^>]*'
    r'href="[^"]*messageAttachmentId=(\d+)[^"]*"[^>]*>'
    r'\s*([^<\s][^<]*?)\s*</nef-link>',
    re.S | re.I,
)

# Whitespace collapse
_WS_RE = re.compile(r'\s+')

# Legal suffixes to strip for Finnish company name normalisation
_FI_SUFFIX_RE = re.compile(
    r'\s+(?:oyj|oy|abp|plc)\s*$',
    re.I,
)

# Date/time in the format "2026-06-09 18:30:00 EEST" — drop the trailing tz word
_DATE_STRIP_TZ_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}(?:\s+\S+)?$')

# ---------------------------------------------------------------------------
# doc_type mapping
# ---------------------------------------------------------------------------

# Maps category label substrings (casefolded) → DOC_TYPES member.
# Order is significant — first match wins. Labels are ENGLISH (from search results).
_LABEL_TO_DOC_TYPE: list[tuple[tuple[str, ...], str]] = [
    (("annual financial report", "annual report"), "annual_report"),
    (("half year",), "half_year_report"),
    (("interim report", "financial statement release"), "interim_statement"),
    (("inside information",), "inside_information"),
    (("major shareholder", "managers' transactions", "managers transactions",
      "total number of voting rights", "total voting rights", "voting rights"),
     "holding_notification"),
]


def _doc_type(category: str) -> str:
    """Map OAM English category label → DOC_TYPES member. Case-insensitive."""
    low = (category or "").casefold().strip()
    for needles, dt in _LABEL_TO_DOC_TYPE:
        if any(n in low for n in needles):
            assert dt in DOC_TYPES, f"bad doc_type constant: {dt!r}"
            return dt
    return "other"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _normalise_name(name: str) -> str:
    """Collapse whitespace, casefold, strip diacritics, strip trailing legal suffix."""
    n = _WS_RE.sub(" ", name).strip()
    n = "".join(c for c in unicodedata.normalize("NFKD", n) if not unicodedata.combining(c))
    n = n.casefold()
    n = _FI_SUFFIX_RE.sub("", n).strip()
    return n


def _parse_published_ts(raw: str) -> str | None:
    """'2026-06-09 18:30:00 EEST' → '2026-06-09', or None on failure."""
    raw = (raw or "").strip()
    m = _DATE_STRIP_TZ_RE.match(raw)
    if m:
        return m.group(1)
    # Fallback: try to grab just the date part
    date_m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if date_m:
        try:
            datetime.fromisoformat(date_m.group(1))
            return date_m.group(1)
        except ValueError:
            pass
    return None


def _parse_options_json(tag_html: str) -> dict[str, int]:
    """Extract and decode the options="..." attribute from a nef-form-select tag.

    Returns a dict mapping label → int(value).
    """
    m = _OPTIONS_ATTR_RE.search(tag_html)
    if not m:
        return {}
    raw = html.unescape(m.group(1))
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    result: dict[str, int] = {}
    for item in items:
        if isinstance(item, dict) and "value" in item and "label" in item:
            try:
                result[item["label"]] = int(item["value"])
            except (ValueError, TypeError):
                pass
    return result


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class OamFI(OamSource):
    """Finland OAM backend — scrapes Nasdaq Helsinki's oam.fi.

    Bootstrap GET oam.fi/ once per instance to get CSRF + company list.
    Resolves entity name → OAM integer company id (exact normalised match).
    Paginates POST / search (1-indexed pages) → hops /view/{view_id} → emits Documents
    with viewAttachment.action file URLs.
    """

    name = "oam-fi"
    country = "FI"

    def __init__(self, fetcher=None, config=None):
        super().__init__(fetcher=fetcher, config=config)
        # Cached bootstrap tuple: (csrf, company_map)
        self._bootstrap_cache: tuple[str, dict[str, int]] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_issuers(self) -> list[IssuerRef]:
        """Return registered issuers from the embedded company list."""
        result = self._bootstrap()
        if result is None:
            return []
        _, company_map = result
        return [
            IssuerRef(lei=None, name=name, country="FI", native_id=str(cid))
            for name, cid in company_map.items()
        ]

    def discover(self, entity: Entity) -> list[Document]:
        if not entity.name:
            return []

        bootstrap = self._bootstrap()
        if bootstrap is None:
            return []
        csrf, company_map = bootstrap

        company_id = self._resolve_company_id(entity, company_map)
        if company_id is None:
            return []

        now = datetime.now(timezone.utc).isoformat()
        return self._paginate_and_discover(csrf, company_id, entity, now)

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def _bootstrap(self) -> tuple[str, dict[str, int]] | None:
        """GET oam.fi/ → (csrf, company_map). Cached after first call."""
        if self._bootstrap_cache is not None:
            return self._bootstrap_cache

        try:
            page_html = self.fetcher.get_text(_BASE + "/")
        except Exception as exc:  # noqa: BLE001
            self._record_error("bootstrap", _BASE + "/", exc)
            return None

        csrf_m = _CSRF_RE.search(page_html)
        if not csrf_m:
            self._record_error(
                "bootstrap-csrf", _BASE + "/",
                RuntimeError("could not find _csrf meta tag in oam.fi bootstrap page"),
            )
            return None
        csrf = csrf_m.group(1)

        # Parse company list from nef-form-select#company-select options attribute
        company_tag_m = _COMPANY_SELECT_TAG_RE.search(page_html)
        if company_tag_m:
            company_map = _parse_options_json(company_tag_m.group(0))
        else:
            company_map = {}

        if not company_map:
            self._record_error(
                "bootstrap-companies", _BASE + "/",
                RuntimeError("no companies found in oam.fi bootstrap page"),
            )
            return None

        self._bootstrap_cache = (csrf, company_map)
        return self._bootstrap_cache

    # ------------------------------------------------------------------
    # Name → company id resolution
    # ------------------------------------------------------------------

    def _resolve_company_id(
        self, entity: Entity, company_map: dict[str, int]
    ) -> int | None:
        """Exact normalised match of entity.name against the OAM company list.

        Returns the integer OAM company id, or None if 0 or >1 matches (error recorded).
        """
        norm_target = _normalise_name(entity.name)
        matches: list[tuple[str, int]] = [
            (raw_name, cid)
            for raw_name, cid in company_map.items()
            if _normalise_name(raw_name) == norm_target
        ]

        if len(matches) == 1:
            return matches[0][1]

        if not matches:
            self._record_error(
                "resolve-no-match",
                _BASE + "/",
                RuntimeError(
                    f"no exact company match for '{entity.name}' "
                    f"(normalised: '{norm_target}') in oam.fi company list"
                ),
            )
        else:
            self._record_error(
                "resolve-ambiguous",
                _BASE + "/",
                RuntimeError(
                    f"ambiguous company match for '{entity.name}': "
                    f"{[r for r, _ in matches]}"
                ),
            )
        return None

    # ------------------------------------------------------------------
    # Paginated search (pages are 1-indexed)
    # ------------------------------------------------------------------

    def _paginate_and_discover(
        self, csrf: str, company_id: int, entity: Entity, now: str
    ) -> list[Document]:
        """POST paginated searches (1-indexed) and collect Documents."""
        docs: list[Document] = []
        page = 1  # OAM uses 1-indexed pages (page=0 returns HTTP 400)
        total_seen = 0

        while True:
            post_data = {
                "_csrf": csrf,
                "oam": "fi",
                "language": "en",
                "pageSize": str(_PAGE_SIZE),
                "page": str(page),
                "company": str(company_id),
            }
            try:
                page_html = self.fetcher.post_text(_SEARCH_URL, post_data)
            except Exception as exc:  # noqa: BLE001
                self._record_error("search", _SEARCH_URL, exc)
                break

            page_docs, row_count, total_data_length = self._parse_search_page(
                page_html, entity, now
            )
            docs.extend(page_docs)
            total_seen += row_count

            # Terminate if no rows on this page
            if row_count == 0:
                break

            # Terminate if we've consumed all available rows
            if total_data_length is not None and total_seen >= total_data_length:
                break

            page += 1
            if page > _MAX_PAGES:
                if total_data_length is not None and total_seen < total_data_length:
                    self._record_error(
                        "truncated",
                        _SEARCH_URL,
                        RuntimeError(
                            f"search pagination hit the {_MAX_PAGES}-page cap; "
                            f"{total_seen}/{total_data_length} rows consumed"
                        ),
                    )
                break

        return docs

    def _parse_search_page(
        self, page_html: str, entity: Entity, now: str
    ) -> tuple[list[Document], int, int | None]:
        """Parse one search-results page.

        Returns (documents, row_count, total_data_length_or_None).
        """
        docs: list[Document] = []

        # Extract pagination total
        pag_m = _PAGINATION_RE.search(page_html)
        total_data_length = int(pag_m.group(1)) if pag_m else None

        # Parse result rows
        row_blocks = _ROW_BLOCK_RE.findall(page_html)

        for block in row_blocks:
            link_m = _ROW_LINK_RE.search(block)
            if not link_m:
                continue
            view_id = link_m.group(1)
            title = html.unescape(link_m.group(2).strip())

            date_m = _ROW_DATE_RE.search(block)
            raw_date = date_m.group(1).strip() if date_m else ""

            cat_m = _ROW_CATEGORY_RE.search(block)
            category = html.unescape(cat_m.group(1).strip()) if cat_m else ""

            doc = self._hop_view_and_build(view_id, title, raw_date, category, entity, now)
            if doc is not None:
                docs.append(doc)

        return docs, len(row_blocks), total_data_length

    # ------------------------------------------------------------------
    # View-hop and Document construction
    # ------------------------------------------------------------------

    def _hop_view_and_build(
        self,
        view_id: str,
        title: str,
        raw_date: str,
        category: str,
        entity: Entity,
        now: str,
    ) -> Document | None:
        """GET /view/{view_id} to collect attachment ids, then build a Document."""
        view_url = _VIEW_URL.format(view_id=view_id)
        try:
            view_html = self.fetcher.get_text(view_url)
        except Exception as exc:  # noqa: BLE001
            self._record_error("view", view_url, exc)
            return None

        files = self._parse_attachments(view_html)
        published_ts = _parse_published_ts(raw_date)

        return Document(
            doc_id=f"fi-{view_id}",
            lei=entity.lei,
            country="FI",
            doc_type=_doc_type(category),
            period_end=None,
            published_ts=published_ts,
            discovered_ts=now,
            language=None,
            source=self.name,
            files=files,
            native_meta={
                "title": title,
                "view_id": view_id,
                "category": category,
                "raw_date": raw_date,
            },
        )

    def _parse_attachments(self, view_html: str) -> list[dict]:
        """Parse attachment links from a /view page."""
        files: list[dict] = []
        for att_id, name in _ATTACHMENT_RE.findall(view_html):
            name = name.strip() or f"attachment-{att_id}"
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            kind = "esef" if ext in ("zip", "xhtml") else "document"
            url = _ATTACHMENT_URL.format(att_id=att_id)
            files.append({"name": name, "kind": kind, "url": url})
        return files
