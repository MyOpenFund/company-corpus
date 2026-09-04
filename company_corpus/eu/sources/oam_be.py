"""FSMA STORI backend — Belgium.

The Belgian OAM (FSMA / STORI) is a modern JSON API at
``https://webapi.fsma.be/api/v1/{lang}/stori`` (lang = fr/en/nl). Identity is via
the issuer's **ISIN** (the cleanest resolution, like the UK LEI); if the entity has
no ISIN we fall back to resolving its name -> ``companyId`` via the abbreviated-name
company list (strict exact match, no guessing).

* **Search:** ``POST {base}/result`` with a JSON body filtered by ``isinCode`` (or
  ``companyId``). Response: ``{"resultCount": N, "storiResultItems": [ITEM, ...]}``.
  Paginated via ``startRowIndex`` / ``pageSize``.
* **Download:** ``GET {base}/download?fileDataId=<GUID>`` — stateless, the filename
  rides ``Content-Disposition``. ``.pdf`` -> document; ``.zip``/``.xhtml``/xbri -> ESEF.

The API sits behind an **F5 BIG-IP ASM WAF** that resets/blocks non-browser clients,
so the live HTTP layer impersonates Chrome via ``curl_cffi`` with browser
``Origin``/``Referer`` headers and a cookie-bootstrapped session. That dependency is
imported lazily so the package still imports without it (other backends keep working),
and tests inject a stub ``http`` client to stay network-free.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

_BASE = "https://webapi.fsma.be/api/v1/fr/stori"
_PAGE = 50
_MAX_RESULTS = 5000

# Browser headers — the F5 WAF resets anything that doesn't look like a real Chrome.
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.fsma.be",
    "Referer": "https://www.fsma.be/",
}

# Files whose type indicates an ESEF/inline-XBRL package rather than a plain document.
_ESEF_TYPES = {"zip", "xbri", "xhtml"}

# ---------------------------------------------------------------------------
# doc_type mapping (case-insensitive, accent-tolerant substring match)
# ---------------------------------------------------------------------------

# Order matters: earlier rules win. Keys are already accent-folded + lowercased.
_TOPIC_RULES: list[tuple[str, str]] = [
    ("rapport financier annuel", "annual_report"),
    ("rapport financier semestriel", "half_year_report"),
    ("information trimestrielle", "interim_statement"),
    ("rapport financier trimestriel", "interim_statement"),
    ("declaration intermediaire", "interim_statement"),
    ("information privilegiee", "inside_information"),
    ("assemblee generale", "governance"),  # Convocation / Procès-verbal
    ("notification de transparence", "holding_notification"),
    ("changement du denominateur", "holding_notification"),
    ("prospectus", "prospectus"),
]


def _fold(text: str) -> str:
    """Lowercase and strip diacritics for accent-tolerant matching."""
    t = (text or "").strip().casefold()
    return "".join(
        c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)
    )


def _doc_type(reporting_topic_name: str) -> str:
    """Map a STORI ``reportingTopicName`` to a ``DOC_TYPES`` member."""
    t = _fold(reporting_topic_name)
    for keyword, mapped in _TOPIC_RULES:
        if keyword in t:
            return mapped
    return "other"


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class StoriBE(OamSource):
    """Belgium OAM backend — FSMA STORI JSON API.

    Searches by the issuer's ISIN(s) (``isinCode``); falls back to a strict
    name -> ``companyId`` resolution against the abbreviated-name company list when
    the entity has no ISIN. No fuzzy matching: an ambiguous/absent name skips.
    """

    name = "oam-be"
    country = "BE"

    def __init__(self, fetcher=None, config=None, http=None):
        super().__init__(fetcher=fetcher, config=config)
        # Injected stub (tests) or a lazily-built curl_cffi session (live).
        self._http = http
        self._session = None  # the curl_cffi session, built on first live use
        self._companies_cache: dict[str, list[str]] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self, entity: Entity) -> list[Document]:
        now = datetime.now(timezone.utc).isoformat()

        # Identity: the entity's ISIN(s) first, else a strict name -> companyId.
        isins = [i for i in (getattr(entity, "isins", ()) or ()) if i]
        filters: list[dict] = [{"isinCode": isin} for isin in isins]
        if not filters:
            company_id = self._resolve_company_id(entity.name)
            if company_id:
                filters = [{"companyId": company_id}]
        if not filters:
            return []

        seen: set[str] = set()
        fetched_company_numbers: set[str] = set()
        out: list[Document] = []
        for filt in filters:
            # Peek: one request at pageSize=1 to read the companyNumber before
            # committing to full pagination.  If the company was already fully
            # fetched for a previous ISIN, skip the full pagination (the docs are
            # already captured). Falls through to full pagination on peek errors.
            if "isinCode" in filt:
                company_number, skip = self._peek_company_number(
                    filt, fetched_company_numbers
                )
                if skip:
                    continue
            try:
                docs = self._search(filt, entity, now, seen)
            except Exception as exc:  # noqa: BLE001
                self._record_error("search", f"{_BASE}/result", exc)
                continue
            out.extend(docs)
            # Record every companyNumber seen in this ISIN's full fetch so that
            # subsequent ISINs mapping to the same company are skipped.
            if "isinCode" in filt:
                for d in docs:
                    cn = (d.native_meta or {}).get("companyNumber")
                    if cn:
                        fetched_company_numbers.add(str(cn))
                # Also record from a successful peek (covers the case of 0 docs).
                if company_number is not None:
                    fetched_company_numbers.add(company_number)
        return out

    # ------------------------------------------------------------------
    # Peek helper
    # ------------------------------------------------------------------

    def _peek_company_number(
        self, filt: dict, fetched: set[str]
    ) -> tuple[str | None, bool]:
        """POST pageSize=1 to read the ``companyNumber`` without full pagination.

        Returns ``(company_number, skip)`` where:
        - ``company_number`` is the string from the first item (or None on error /
          empty response).
        - ``skip`` is True when the company was already fully fetched and the caller
          should skip full pagination.

        A peek error is recorded and returns ``(None, False)`` so the caller falls
        through to a normal full pagination (never silently drops an ISIN).
        """
        body = {
            "startRowIndex": 0,
            "pageSize": 1,
            "sortDirection": "Descending",
            **filt,
        }
        try:
            resp = self._post_json(f"{_BASE}/result", body)
        except Exception as exc:  # noqa: BLE001
            self._record_error("peek", f"{_BASE}/result", exc)
            return None, False

        items = resp.get("storiResultItems") or []
        result_count = resp.get("resultCount")
        # Skip (empty ISIN) only when no items AND count is absent-or-zero.
        # A missing resultCount must not cause us to skip an ISIN that has items.
        if not items and (result_count is None or int(result_count) == 0):
            return None, True  # empty ISIN — skip silently, no error
        if not items:
            # resultCount > 0 but no items in peek response — fall through to full search.
            return None, False

        company_number = str(items[0].get("companyNumber") or "").strip() or None
        if company_number and company_number in fetched:
            return company_number, True  # already fully fetched — skip

        return company_number, False

    # ------------------------------------------------------------------
    # Search + pagination
    # ------------------------------------------------------------------

    def _search(
        self, filt: dict, entity: Entity, now: str, seen: set[str]
    ) -> list[Document]:
        """Paginate one identity filter (isinCode or companyId)."""
        docs: list[Document] = []
        start = 0
        total: int | None = None

        while True:
            body = {
                "startRowIndex": start,
                "pageSize": _PAGE,
                "sortDirection": "Descending",
                **filt,
            }
            try:
                resp = self._post_json(f"{_BASE}/result", body)
            except Exception as exc:  # noqa: BLE001
                self._record_error("page", f"{_BASE}/result", exc)
                break

            if total is None:
                rc = resp.get("resultCount")
                if rc is not None:
                    total = int(rc)
                # When resultCount is absent, total stays None and pagination is
                # driven by empty pages instead of a count — mirrors FI/IT pattern.
                if total is not None and total > _MAX_RESULTS:
                    self._record_error(
                        "truncated",
                        f"{_BASE}/result",
                        RuntimeError(
                            f"resultCount={total} exceeds _MAX_RESULTS={_MAX_RESULTS}; "
                            "results truncated"
                        ),
                    )

            items = resp.get("storiResultItems") or []
            if not items:
                break

            for item in items:
                doc = self._item_to_doc(item, entity, now, seen)
                if doc is not None:
                    docs.append(doc)

            start += _PAGE
            if start >= _MAX_RESULTS:
                break
            if total is not None and start >= total:
                break

        return docs

    def _item_to_doc(
        self, item: dict, entity: Entity, now: str, seen: set[str]
    ) -> Document | None:
        """Build one Document from a STORI result item (dedup by topic id)."""
        topic_id = item.get("requiredReportingTopicId")
        if topic_id:
            if topic_id in seen:
                return None
            seen.add(topic_id)

        files: list[dict] = []
        for entry in (item.get("mainDocuments") or []) + (item.get("attachments") or []):
            file_data_id = entry.get("fileDataId")
            if not file_data_id:
                continue
            file_type = (entry.get("fileType") or "").lower()
            files.append({
                "name": entry.get("originalFileName"),
                "kind": "esef" if file_type in _ESEF_TYPES else "document",
                "url": f"{_BASE}/download?fileDataId={file_data_id}",
                "language": entry.get("language"),
            })

        # requiredReportingTopicId is the primary key; fall back to a stable, unique
        # id (the first fileDataId — a GUID) so distinct keyless items never collide.
        fallback = files[0]["url"].rsplit("fileDataId=", 1)[-1] if files else None
        doc_id = f"be-{topic_id or fallback or item.get('dateReceived') or now}"
        return Document(
            doc_id=doc_id,
            lei=item.get("lei") or entity.lei,
            country="BE",
            doc_type=_doc_type(item.get("reportingTopicName") or ""),
            period_end=None,
            published_ts=item.get("datePublication"),
            discovered_ts=now,
            language=None,
            source=self.name,
            files=files,
            native_meta={
                "reportingTopicName": item.get("reportingTopicName"),
                "companyName": item.get("companyName"),
                "companyNumber": item.get("companyNumber"),
                "documentTitle": item.get("documentTitle"),
                "isinCodes": item.get("isinCodes"),
            },
        )

    # ------------------------------------------------------------------
    # Name -> companyId resolution (strict, no guessing)
    # ------------------------------------------------------------------

    def _resolve_company_id(self, name: str) -> str | None:
        """Resolve an issuer name to its STORI ``companyId`` via exact, normalised
        match on the abbreviated-name list. Returns None (and records an error) if
        there is no match or more than one."""
        if not name:
            return None
        key = _fold(name)

        if self._companies_cache is None:
            try:
                rows = self._get_json(f"{_BASE}/companies/abbreviated-name")
            except Exception as exc:  # noqa: BLE001
                self._record_error("companies", f"{_BASE}/companies/abbreviated-name", exc)
                return None
            cache: dict[str, list[str]] = {}
            for row in rows or []:
                abbr = _fold(row.get("abbreviation") or "")
                cid = row.get("companyId")
                if abbr and cid:
                    cache.setdefault(abbr, []).append(cid)
            self._companies_cache = cache

        ids = self._companies_cache.get(key) or []
        if len(ids) == 1:
            return ids[0]
        self._record_error(
            "resolve-name",
            f"{_BASE}/companies/abbreviated-name",
            RuntimeError(
                f"no unique companyId for name {name!r} "
                f"(normalised {key!r}); matches={ids}"
            ),
        )
        return None

    # ------------------------------------------------------------------
    # HTTP layer (curl_cffi / F5 WAF) — bypassed when `http` is injected
    # ------------------------------------------------------------------

    def _post_json(self, url: str, body: dict) -> dict:
        if self._http is not None:
            return self._http.post_json(url, body)
        session = self._ensure_session()
        if session is None:
            return {}
        resp = session.post(url, json=body, headers=_HEADERS)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"HTTP {resp.status_code} from {url}")
        return resp.json()

    def _get_json(self, url: str) -> object:
        if self._http is not None:
            return self._http.get_json(url)
        session = self._ensure_session()
        if session is None:
            return None
        resp = session.get(url, headers=_HEADERS)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"HTTP {resp.status_code} from {url}")
        return resp.json()

    def _ensure_session(self):
        """Lazily build a Chrome-impersonating curl_cffi session and bootstrap the
        F5 ``TS…`` cookie with one GET. Returns None (recording a dependency error)
        if curl_cffi is unavailable, so the rest of the package keeps working."""
        if self._session is not None:
            return self._session
        try:
            from curl_cffi import requests as cffi_requests  # lazy import
        except Exception as exc:  # noqa: BLE001
            self._record_error(
                "dependency",
                _BASE,
                RuntimeError(f"curl_cffi is required for the BE backend: {exc}"),
            )
            return None
        session = cffi_requests.Session(impersonate="chrome124")
        # Bootstrap: one GET to obtain the F5 cookie before the first POST.
        try:
            session.get(f"{_BASE}/document-type", headers=_HEADERS)
        except Exception as exc:  # noqa: BLE001
            self._record_error("bootstrap", f"{_BASE}/document-type", exc)
        self._session = session
        return session
