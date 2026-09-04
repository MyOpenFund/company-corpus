"""Switzerland disclosure backend — SIX Swiss Exchange + EQS News aggregator.

Switzerland is not in the EU, so it has no statutory OAM: ad hoc announcements
are stored in a decentralised way (each issuer picks a disseminator and mirrors
on its own site).  No single public archive is complete.  This backend unions
the two clean public sources to widen coverage, keyed on the entity's GLEIF
ISINs (no-guess identity throughout):

* **SIX** — the Share Explorer per-ISIN disclosure feed
  (``share_details.equityissuer.json``).  Exhaustive for the issuers that route
  through SIX's own distribution; returns the announcement HTML inline plus the
  attachment PDFs.  Queried directly by ISIN.
* **EQS News** (``eqs-news.com``) — the dominant DACH disseminator.  Queried by
  name then **verified by ISIN** (``data-news-isin`` must be one of the entity's
  ISINs) before any of its filings are trusted, then paged exhaustively through
  the per-company feed.  Adds issuers SIX misses (e.g. Logitech) and deeper
  history.

The two feeds overlap (both back onto the same eqs-cockpit/schedulr PDF host),
so announcements are de-duplicated across providers by ``(title, date)``.

Issuers that self-distribute (Novartis, Roche, UBS, Zürich…) are in neither
public archive — they yield no documents here, surfaced by the coverage report
as ``no-documents`` (never a silent gap).  Plain ``requests`` works for both
hosts; neither sits behind the F5 WAF that guards the legacy SIX ad-hoc tooling.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

# ---------------------------------------------------------------------------
# SIX constants
# ---------------------------------------------------------------------------

_SIX_FEED_URL = (
    "https://www.six-group.com/en/market-data/shares/share-explorer/share-details"
    "/_jcr_content/sections/section/content/share_details.equityissuer.json"
)
_SIX_FROM = "20000101"
_SIX_TO = "20401231"
_SIX_PAGE = 100
_SIX_MAX_PAGES = 200  # 20k items/issuer — a backstop, never reached in practice.

# ---------------------------------------------------------------------------
# EQS constants
# ---------------------------------------------------------------------------

_EQS_AJAX = "https://www.eqs-news.com/wp/wp-admin/admin-ajax.php"
_EQS_PAGE = 30
_EQS_MAX_PAGES = 80  # 2.4k items/issuer — a backstop, recorded if ever hit.

# Trailing legal-form tokens. EQS search matches the issuer's *trade* name, so
# the GLEIF legal suffix ("LOGITECH INTERNATIONAL S.A.") must be stripped to
# surface the issuer's cards; distinctive words are kept. Precision still comes
# from the per-card ISIN check, so a broader query never mis-binds.
_EQS_LEGAL_SUFFIX_RE = re.compile(
    r"[\s,]+(?:S\.?A\.?|AG|SE|N\.?V\.?|Ltd\.?|Limited|PLC|Inc\.?|Oyj|ASA|AB)\s*$", re.I
)


def _eqs_query(name: str) -> str:
    """Reduce a GLEIF legal name to the EQS-searchable trade name."""
    s = name or ""
    for _ in range(3):  # peel stacked forms, e.g. "… Holding AG"
        s = _EQS_LEGAL_SUFFIX_RE.sub("", s).strip()
    return re.sub(r"\s+", " ", s.replace(".", " ")).strip()

# Attachment links embedded in announcement HTML (both feeds use this host).
_ATTACH_RE = re.compile(r"https://(?:app\.schedulr\.ch|eqs-cockpit\.com)/[^\s\"'<>]+")

# Card anchor marker, and the per-card fields (extracted within a card slice).
_EQS_CARD_ANCHOR_RE = re.compile(r'data-wio="news-feed-list-item"')
_EQS_CARD_URL_RE = re.compile(r'data-news-url="([^"]+)"')
_EQS_CARD_ISIN_RE = re.compile(r'data-news-isin="([^"]*)"')
_EQS_CARD_TITLE_RE = re.compile(r'news__heading[^>]*>\s*([^<]+?)\s*<')
# A grouped date header preceding a run of cards, e.g. "25 June 2026".
_EQS_DATE_RE = re.compile(r'news__date[^>]*>\s*([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})\s*<')
# The company uuid carried on a search card, paired with its ISIN.
_EQS_SEARCH_CARD_RE = re.compile(
    r'data-news-uuid="(?P<uuid>[0-9a-f-]+)"'
    r'[\s\S]{0,400}?data-news-isin="(?P<isin>[^"]*)"'
    r'[\s\S]{0,4000}?news__company">(?P<name>[^<]+)<',
    re.I,
)

# ---------------------------------------------------------------------------
# doc_type mapping
# ---------------------------------------------------------------------------

# Order matters: earlier (more specific) rules win. Matched against the title.
_TYPE_RULES: list[tuple[str, str]] = [
    ("annual report", "annual_report"),
    ("annual reporting", "annual_report"),
    ("geschäftsbericht", "annual_report"),
    ("half-year", "half_year_report"),
    ("half year", "half_year_report"),
    ("semi-annual", "half_year_report"),
    ("halbjahr", "half_year_report"),
    ("interim", "interim_statement"),
    ("quarter", "interim_statement"),
    ("results", "interim_statement"),
    ("prospectus", "prospectus"),
    ("capital increase", "prospectus"),
    ("annual general meeting", "governance"),
    ("general meeting", "governance"),
    ("board of directors", "governance"),
    ("election", "governance"),
    ("voting rights", "holding_notification"),
    ("disclosure of shareholdings", "holding_notification"),
    ("significant shareholder", "holding_notification"),
]


def _doc_type(title: str, ad_hoc: bool) -> str:
    """Map an announcement title (+ the ad_hoc flag) to a ``DOC_TYPES`` member.

    Title keywords take priority; an item flagged ``ad_hoc`` that matches no
    report/governance rule is inside information (Art. 53 LR), else ``other``.
    """
    t = (title or "").lower()
    for keyword, mapped in _TYPE_RULES:
        if keyword in t:
            return mapped
    return "inside_information" if ad_hoc else "other"


def _ts_from_millis(news_date) -> str | None:
    """Convert a SIX ``news_date`` (epoch milliseconds) to an ISO-8601 string."""
    if not isinstance(news_date, (int, float)):
        return None
    return datetime.fromtimestamp(news_date / 1000, tz=timezone.utc).isoformat()


def _ts_from_eqs_date(text: str) -> str | None:
    """Convert an EQS date header (``25 June 2026``) to an ISO-8601 string."""
    try:
        d = datetime.strptime(text.strip(), "%d %B %Y").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return d.isoformat()


def _dedup_key(title: str, published_ts: str | None) -> tuple[str, str]:
    """Provider-independent identity for one announcement: title + calendar day.

    The same announcement reaches SIX and EQS with identical wording and date,
    so normalising the title (lower-cased, whitespace-collapsed) and keeping only
    the ``YYYY-MM-DD`` prefix of the timestamp collapses the two into one.
    """
    norm = re.sub(r"\s+", " ", (title or "").strip().lower())
    day = (published_ts or "")[:10]
    return (norm, day)


class DisclosureCH(OamSource):
    """Switzerland backend — unions the SIX and EQS public disclosure feeds.

    Both providers are driven from the entity's GLEIF ISINs (SIX directly, EQS
    via name-search verified against those ISINs), and their announcements are
    merged with SIX taking precedence on overlap.
    """

    name = "oam-ch"
    country = "CH"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self, entity: Entity) -> list[Document]:
        """Return the union of SIX and EQS disclosures for *entity*.

        With no ISIN there is no query key for either provider, so an error is
        recorded and ``[]`` returned (never a silent empty).
        """
        isins = self._candidate_isins(entity)
        if not isins:
            self._record_error(
                "no-isin",
                _SIX_FEED_URL,
                RuntimeError(
                    f"entity {entity.name!r} has no ISIN; both CH feeds are ISIN-keyed "
                    "and cannot be queried without one"
                ),
            )
            return []

        now = datetime.now(timezone.utc).isoformat()
        seen: set[tuple[str, str]] = set()
        docs: list[Document] = []

        # SIX first: it carries the richer metadata (the ad_hoc flag) and its
        # attachments come inline (no extra fetch), so it wins on any
        # announcement both providers report.
        for doc, key in self._six_documents(entity, isins, now):
            if key in seen:
                continue
            seen.add(key)
            docs.append(doc)
        # EQS: enumerate items cheaply (search + feed pages only), drop the ones
        # SIX already has, and fetch the article page *only* for the survivors —
        # so a deep back-catalogue that overlaps SIX costs no extra requests.
        for url, isin, title, published_ts in self._eqs_enumerate(entity, isins):
            key = _dedup_key(title, published_ts)
            if key in seen:
                continue
            seen.add(key)
            docs.append(
                self._eqs_build_document(entity, url, isin, title, published_ts, now)
            )
        return docs

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _candidate_isins(entity: Entity) -> list[str]:
        """Entity ISINs ordered Swiss-equity-first, de-duplicated."""
        isins = [i for i in (entity.isins or ()) if i]
        ch_first = sorted(isins, key=lambda i: (not i.upper().startswith("CH"),))
        seen: set[str] = set()
        ordered: list[str] = []
        for i in ch_first:
            if i not in seen:
                seen.add(i)
                ordered.append(i)
        return ordered

    # ------------------------------------------------------------------
    # Provider: SIX Swiss Exchange
    # ------------------------------------------------------------------

    def _six_documents(self, entity: Entity, isins: list[str], now: str):
        """Yield ``(Document, dedup_key)`` for every SIX item across the ISINs."""
        seen_ids: set[str] = set()
        for isin in isins:
            for item in self._six_fetch_isin(isin):
                item_id = str(item.get("id") or "")
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)
                built = self._six_to_document(item, entity, isin, now)
                if built is not None:
                    yield built

    def _six_fetch_isin(self, isin: str) -> list[dict]:
        """Page the SIX disclosure feed for one ISIN; return raw items."""
        items: list[dict] = []
        total: int | None = None
        for page in range(_SIX_MAX_PAGES):
            url = (
                f"{_SIX_FEED_URL}?from={_SIX_FROM}&to={_SIX_TO}"
                f"&pageNumber={page}&pageSize={_SIX_PAGE}&isin={isin}"
            )
            try:
                resp = self.fetcher.get_json(url)
            except Exception as exc:  # noqa: BLE001
                self._record_error("six-search", url, exc)
                break
            if total is None:
                t = resp.get("total")
                if t is not None:
                    total = t
                # When "total" is absent, pagination is driven by empty pages
                # (mirrors FI/IT pattern) rather than a possibly-missing count.
            batch = resp.get("data") or []
            if not batch:
                break
            items.extend(batch)
            if total is not None and len(items) >= total:
                break
        else:
            self._record_error(
                "six-truncated",
                _SIX_FEED_URL,
                RuntimeError(f"ISIN {isin}: hit the {_SIX_MAX_PAGES}-page cap; truncated"),
            )
        return items

    def _six_to_document(self, item: dict, entity: Entity, isin: str, now: str):
        """Build ``(Document, dedup_key)`` from one SIX feed item, or ``None``."""
        item_id = str(item.get("id") or "")
        contents = item.get("content") or []
        primary = contents[0] if contents else {}
        title = primary.get("title") or ""
        body = primary.get("content") or ""
        language = primary.get("language")
        ad_hoc = bool(item.get("ad_hoc"))
        published_ts = _ts_from_millis(item.get("news_date"))

        files: list[dict] = []
        if body:
            files.append({"name": f"ch-{item_id}.html", "kind": "announcement", "content": body})
        for n, url in enumerate(dict.fromkeys(_ATTACH_RE.findall(body))):
            files.append({"name": f"ch-{item_id}-att{n}.pdf", "kind": "document", "url": url})
        if not files:
            return None

        doc = Document(
            doc_id=f"ch-six-{item_id}" if item_id else f"ch-six-{isin}-{len(files)}",
            lei=entity.lei,
            country="CH",
            doc_type=_doc_type(title, ad_hoc),
            period_end=None,
            published_ts=published_ts,
            discovered_ts=now,
            language=language,
            source=self.name,
            files=files,
            native_meta={
                "provider": "six",
                "title": title,
                "ad_hoc": ad_hoc,
                "isin": isin,
                "company": (item.get("company") or {}).get("name"),
                "six_id": item_id,
            },
        )
        return doc, _dedup_key(title, published_ts)

    # ------------------------------------------------------------------
    # Provider: EQS News
    # ------------------------------------------------------------------

    def _eqs_enumerate(self, entity: Entity, isins: list[str]):
        """Yield ``(article_url, isin, title, published_ts)`` for the entity's EQS feed.

        Cheap: resolves the EQS company id by name-search *verified* against the
        entity's ISINs (no-guess), then pages the per-company feed. No article is
        fetched here — that is deferred to the survivors of cross-provider dedup.
        """
        if not entity.name:
            return
        company_id, company_name, _isin = self._eqs_resolve_company(entity.name, set(isins))
        if not company_id:
            return  # entity not present in EQS — not an error, just no coverage
        yield from self._eqs_company_items(company_id, company_name)

    def _eqs_build_document(
        self, entity: Entity, url: str, isin: str, title: str, published_ts: str | None, now: str
    ) -> Document:
        """Build a :class:`Document` from one EQS feed item.

        The EQS article page *is* the canonical disclosure — its full announcement
        text lives there, and (unlike SIX's inline feed) the page chrome makes any
        embedded ``eqs-cockpit`` link indistinguishable from share/tracking links.
        So the article URL is stored as the document; the announcement text is
        captured by downloading that page (no per-item fetch at discovery, which
        keeps a deep back-catalogue cheap to enumerate).
        """
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        return Document(
            doc_id=f"ch-eqs-{slug}",
            lei=entity.lei,
            country="CH",
            doc_type=_doc_type(title, False),
            period_end=None,
            published_ts=published_ts,
            discovered_ts=now,
            language=None,
            source=self.name,
            files=[{"name": "announcement.html", "kind": "announcement", "url": url}],
            native_meta={
                "provider": "eqs",
                "title": title,
                "isin": isin,
                "article_url": url,
            },
        )

    def _eqs_resolve_company(self, name: str, isin_set: set[str]):
        """Return ``(company_id, company_name, isin)`` for the ISIN-matched issuer.

        Searches EQS by name and accepts only a card whose ``data-news-isin`` is
        one of the entity's ISINs — name alone never binds a company (no-guess).
        Returns ``(None, None, None)`` when the entity is absent from EQS.
        """
        params = {
            "lang": "en",
            "action": "fetch_realtime_news_data",
            "recordsFrom[0][api_type]": "news",
            "pageLimit": _EQS_PAGE,
            "pageNo": 1,
            "loadFrom": "mysql",
            "filter[search]": _eqs_query(name),
        }
        try:
            html = self.fetcher.get_text(_EQS_AJAX, params=params)
        except Exception as exc:  # noqa: BLE001
            self._record_error("eqs-search", _EQS_AJAX, exc)
            return None, None, None
        for m in _EQS_SEARCH_CARD_RE.finditer(html):
            if m.group("isin") in isin_set:
                return m.group("uuid"), m.group("name").strip(), m.group("isin")
        return None, None, None

    def _eqs_company_items(self, company_id: str, company_name: str):
        """Yield ``(article_url, isin, title, published_ts)`` for the company feed.

        Pages the per-company endpoint until an empty page; records truncation if
        the page cap is hit (never a silent cut-off).
        """
        for page in range(1, _EQS_MAX_PAGES + 1):
            params = {
                "lang": "en",
                "action": "fetch_eqs_financial_news_data",
                "newsType": "ALL",
                "pageLimit": _EQS_PAGE,
                "pageNo": page,
                "loadFrom": "mysql",
                "companyName": company_name,
                "companyId": company_id,
                "parseBy": "newsgrouping",
            }
            try:
                html = self.fetcher.get_text(_EQS_AJAX, params=params)
            except Exception as exc:  # noqa: BLE001
                self._record_error("eqs-feed", _EQS_AJAX, exc)
                return
            rows = list(self._parse_eqs_cards(html))
            if not rows:
                return
            yield from rows
        self._record_error(
            "eqs-truncated",
            _EQS_AJAX,
            RuntimeError(f"company {company_id}: hit the {_EQS_MAX_PAGES}-page cap; truncated"),
        )

    @staticmethod
    def _parse_eqs_cards(html: str):
        """Yield ``(url, isin, title, published_ts)`` from one EQS feed page.

        Date headers group runs of cards, so the parser walks the page in order,
        carrying the most recent date header onto each following card, and reads
        each card's fields from its own slice (anchor → next marker).
        """
        # Ordered markers: date headers and card anchors.
        markers = [(m.start(), "date", m.group(1)) for m in _EQS_DATE_RE.finditer(html)]
        markers += [(m.start(), "card", None) for m in _EQS_CARD_ANCHOR_RE.finditer(html)]
        markers.sort(key=lambda t: t[0])
        starts = [pos for pos, kind, _ in markers if kind == "card"]

        current_ts: str | None = None
        for i, (pos, kind, payload) in enumerate(markers):
            if kind == "date":
                current_ts = _ts_from_eqs_date(payload)
                continue
            end = next((p for p in starts if p > pos), len(html))
            block = html[pos:end]
            um = _EQS_CARD_URL_RE.search(block)
            im = _EQS_CARD_ISIN_RE.search(block)
            tm = _EQS_CARD_TITLE_RE.search(block)
            if not (um and tm):
                continue
            yield (
                _html.unescape(um.group(1)),
                im.group(1) if im else "",
                _html.unescape(tm.group(1)).strip(),
                current_ts,
            )
