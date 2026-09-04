"""Bundesanzeiger (www.bundesanzeiger.de) backend — Germany.

The Kapitalmarkt area is a no-captcha, server-rendered Apache-Wicket app. There is
no JSON API and no stable document URL: every link is session-bound and positional.
So the flow is, per register:

1. **Bootstrap session**: GET the register URL on the shared ``requests.Session``
   (the Fetcher's session) — this drops a session cookie and lands on a page that
   carries the SEARCH form.
2. **Search**: form-encoded POST to that form's action with ``fulltext=<name>`` and
   ``search-button=`` (empty) → results HTML on ``/pub/de/suchen2``.
3. **Parse result rows**: each ``<div class="row">`` (skipping the header and any
   ``subsidiary_list`` row) carries the publishing entity (``.first``), the register
   label (``.part``), the title + session-bound detail link (``.info > a``), and the
   publication date (``.date``).
4. **Filter to the target issuer**: full-text search is noisy (e.g. ``SAP SE``
   surfaces Bridgewater short-position notices that merely mention SAP). Keep only
   rows whose normalised ``.first`` STARTS WITH the normalised ``entity.name``;
   record how many rows were dropped — never silently.
5. **Capture-at-discovery**: there is no re-fetchable URL, so for each kept row GET
   its detail link IN THE SAME SESSION and store the returned HTML inline via the
   file ``content`` key (download.py writes inline content directly, no re-fetch).
6. **Pagination**: follow pager links up to ``_MAX_PAGES``; if more pages exist,
   record a ``truncated`` error so the incompleteness is visible.

Every network step is wrapped so one failure (one register, one page, one detail
fetch) is recorded and does not abort the rest.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

# The two Kapitalmarkt registers, as (URL slug, register label).
_REGISTERS = [
    ("suche-kapitalmarkt", "kapitalmarkt"),
    ("suche-rechnungslegung", "rechnungslegung"),
]
_BASE = "https://www.bundesanzeiger.de/pub/de/"

# Follow at most this many result pages per register, then record a truncation so the
# incompleteness is visible. Most issuers fit in far fewer pages after the issuer
# filter; a high cap keeps the rare prolific issuer mostly intact.
_MAX_PAGES = 25

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")
# A result row: <div class="row"> or <div class="row back"> but NOT the header
# (result_header) nor a subsidiary list (subsidiary_list). We isolate each row's
# inner HTML up to the NEXT row opening (any class) or end of string — the trailing
# alternative ensures the final result row is not lost when no row follows it.
_ROW_RE = re.compile(r'<div class="row( back)?">(.*?)(?=<div class="row|\Z)', re.S)
_FIRST_RE = re.compile(r'<div class="first">(.*?)</div>', re.S)
_INFO_RE = re.compile(r'<div class="info">\s*<a href="([^"]*)">(.*?)</a>', re.S)
_DATE_RE = re.compile(r'<div class="date">(.*?)</div>', re.S)
_DATE_DMY_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
# A pager link to a numbered page (…pager-navigation-<n>-pagination~link).
_PAGER_RE = re.compile(r'href="([^"]*pager-navigation-\d+-pagination~link)"')
# The search form: the <form action="…"> whose body carries BOTH fulltext and
# search-button. We extract every form, then pick that one.
_FORM_RE = re.compile(r'<form[^>]*action="([^"]*)"[^>]*>(.*?)</form>', re.S)

# Title-substring → DOC_TYPES member. Order matters: more specific first (a
# Halbjahresfinanzbericht must not be caught by the broad annual-report set).
# All targets are members of DOC_TYPES.
_DOC_TYPE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("halbjahresfinanzbericht", "halbjahres"), "half_year_report"),
    (
        ("jahresfinanzbericht", "jahresabschluss", "geschäftsbericht", "konzernabschluss"),
        "annual_report",
    ),
    (("stimmrecht", "gesamtzahl der stimmrechte", "veröffentlichung gem. §"), "holding_notification"),
    (("insiderinformation", "ad hoc", "art. 17"), "inside_information"),
    (("hauptversammlung", "einladung"), "governance"),
]


def _text(html_fragment: str) -> str:
    """Strip tags + HTML entities to whitespace-collapsed plain text."""
    txt = _TAG_RE.sub(" ", html_fragment)
    txt = txt.replace("&nbsp;", " ").replace("&#160;", " ").replace("&amp;", "&")
    return _WS_RE.sub(" ", txt).strip()


def _normalise_entity(name: str) -> str:
    """Collapse whitespace + casefold; the trailing city/country tokens are handled
    by the STARTS-WITH match (the target name is the prefix, the city is the suffix)."""
    return _WS_RE.sub(" ", name).strip().casefold()


def _doc_type(title: str) -> str:
    low = (title or "").casefold()
    for needles, dt in _DOC_TYPE_RULES:
        if any(n in low for n in needles):
            return dt
    return "other"


def _parse_date(date_text: str) -> str | None:
    """DD.MM.YYYY → ISO date string (YYYY-MM-DD), or None if unparseable."""
    m = _DATE_DMY_RE.search(date_text or "")
    if not m:
        return None
    dd, mm, yyyy = m.groups()
    try:
        return datetime(int(yyyy), int(mm), int(dd)).date().isoformat()
    except ValueError:
        return None


class BundesanzeigerDE(OamSource):
    """Germany OAM backend — stateful Wicket scrape of the Bundesanzeiger.

    Keys on the issuer NAME (full-text search + the publishing-entity filter); there
    is no id to resolve.
    """

    name = "oam-de"
    country = "DE"

    def discover(self, entity: Entity) -> list[Document]:
        if not entity.name:
            return []
        now = datetime.now(timezone.utc).isoformat()
        out: list[Document] = []
        for slug, register in _REGISTERS:
            out.extend(self._discover_register(slug, register, entity, now))
        return out

    # ------------------------------------------------------------------
    # Per-register flow
    # ------------------------------------------------------------------

    def _discover_register(
        self, slug: str, register: str, entity: Entity, now: str
    ) -> list[Document]:
        landing_url = _BASE + slug
        try:
            landing = self.fetcher.get_text(landing_url)
        except Exception as exc:  # noqa: BLE001
            self._record_error("bootstrap", landing_url, exc)
            return []

        action = self._find_search_action(landing)
        if not action:
            self._record_error(
                "search-form", landing_url,
                RuntimeError("no search form (fulltext + search-button) on landing"),
            )
            return []

        try:
            results = self.fetcher.post_text(
                action, {"fulltext": entity.name, "search-button": ""}
            )
        except Exception as exc:  # noqa: BLE001
            self._record_error("search", action, exc)
            return []

        docs: list[Document] = []
        norm_target = _normalise_entity(entity.name)
        dropped = 0
        seen_pages: set[str] = set()
        page = 0
        while True:
            page_docs, page_dropped = self._parse_results_page(
                results, register, entity, norm_target, now
            )
            docs.extend(page_docs)
            dropped += page_dropped
            page += 1

            next_url = self._next_page_url(results, seen_pages)
            if not next_url:
                break
            if page >= _MAX_PAGES:
                self._record_error(
                    "truncated", next_url,
                    RuntimeError(
                        f"results exceeded the {_MAX_PAGES}-page cap for {register}; "
                        "remaining pages not crawled"
                    ),
                )
                break
            seen_pages.add(next_url)
            try:
                results = self.fetcher.get_text(next_url)
            except Exception as exc:  # noqa: BLE001
                self._record_error("page", next_url, exc)
                break

        if dropped:
            self._record_error(
                "issuer-filter", action,
                RuntimeError(
                    f"dropped {dropped} row(s) whose publishing entity did not match "
                    f"'{entity.name}' ({register})"
                ),
            )
        return docs

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _find_search_action(self, html: str) -> str | None:
        """Return the action of the <form> carrying both fulltext and search-button."""
        for action, body in _FORM_RE.findall(html):
            if "fulltext" in body and "search-button" in body:
                return action
        return None

    def _parse_results_page(
        self, html: str, register: str, entity: Entity, norm_target: str, now: str
    ) -> tuple[list[Document], int]:
        """Parse one results page → (kept Documents, count of issuer-filtered rows)."""
        docs: list[Document] = []
        dropped = 0
        for _back, row in _ROW_RE.findall(html):
            if "subsidiary_list" in row or "result_header" in row:
                continue
            info = _INFO_RE.search(row)
            if not info:
                continue  # header / structural row without a publication link
            first_m = _FIRST_RE.search(row)
            entity_name = _text(first_m.group(1)) if first_m else ""
            if not entity_name:
                continue

            # Issuer filter: keep only rows the target actually published. Full-text
            # search surfaces disclosures BY OTHER entities that merely mention the
            # target (e.g. short-position notices) — drop those, but never silently.
            if not _normalise_entity(entity_name).startswith(norm_target):
                dropped += 1
                continue

            href, title_html = info.group(1), info.group(2)
            title = _text(title_html)
            date_m = _DATE_RE.search(row)
            published_ts = _parse_date(_text(date_m.group(1)) if date_m else "")

            doc = self._build_document(
                href, title, register, entity, published_ts, entity_name, now
            )
            docs.append(doc)
        return docs, dropped

    def _next_page_url(self, html: str, seen: set[str]) -> str | None:
        """First pager link not already crawled, else None (last page).

        Assumes a page's own pager does not link to itself (verified live: the
        Wicket pager omits the current page's link, so the first unseen link is
        genuinely the next page). The ``seen`` set is the backstop against any
        revisit regardless.
        """
        for url in _PAGER_RE.findall(html):
            if url not in seen:
                return url
        return None

    # ------------------------------------------------------------------
    # Document construction (with capture-at-discovery)
    # ------------------------------------------------------------------

    def _build_document(
        self,
        href: str,
        title: str,
        register: str,
        entity: Entity,
        published_ts: str | None,
        publishing_entity: str,
        now: str,
    ) -> Document:
        doc_id = self._doc_id(entity, published_ts, title, register)
        file_entry: dict = {"name": f"{doc_id}.html", "kind": "html", "url": href}

        # Capture-at-discovery: the detail link has no stable URL, so fetch it now in
        # the same session and store the HTML inline. If the GET fails, still emit the
        # Document (the index entry survives) MINUS content, and record the error.
        try:
            file_entry["content"] = self.fetcher.get_text(href)
        except Exception as exc:  # noqa: BLE001
            self._record_error("detail", href, exc)
            # The href is a session-bound Wicket link; re-fetching it cross-session
            # (at download time) can return an unrelated "session expired" 200 page.
            # Drop it so download.py never persists a stale body — the link survives
            # as provenance in native_meta.detail_url, and the index entry survives.
            file_entry.pop("url", None)
            file_entry["capture_failed"] = True

        return Document(
            doc_id=doc_id,
            lei=entity.lei,
            country="DE",
            doc_type=_doc_type(title),
            period_end=None,  # not reliably available from the listing
            published_ts=published_ts,
            discovered_ts=now,
            language="de",
            source=self.name,
            files=[file_entry],
            native_meta={
                "title": title,
                "register": register,
                "publishing_entity": publishing_entity,
                "detail_url": href,
            },
        )

    @staticmethod
    def _doc_id(entity: Entity, published_ts: str | None, title: str, register: str) -> str:
        """Deterministic id from issuer + date + a short title hash + register.

        Two genuinely distinct publications that share issuer+date+title+register would
        collide and dedupe to one in merge_documents — vanishingly rare given the title
        hash, and acceptable: such pairs are near-certainly the same disclosure.
        """
        ymd = (published_ts or "0000-00-00").replace("-", "")
        key = f"{entity.lei or entity.name}|{ymd}|{title}|{register}"
        short = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        return f"de-{register}-{ymd}-{short}"
