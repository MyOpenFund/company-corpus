"""Finanstilsynet OAM Publication backend — Denmark.

Denmark's OAM (Finanstilsynet) is a public Azure REST API at
``https://weappegressprod.azurewebsites.net``.  Identity is via the issuer's
Danish **CVR number** (8 digits).  There is no LEI or ISIN search — the CVR
is resolved from an exact normalised name match against the issuer dropdown
exposed by ``GET /config``.

* **Issuer list:** ``GET /config`` → ``components.filters[IssuerFilter].options``
  — a list of ``{id: "<cvr>", label: "<company name>"}`` (~471 entries).
  Resolution: collapse whitespace + casefold + strip diacritics + strip
  trailing " a/s".  Strict: 0 or >1 matches → ``_record_error`` + [].

* **Search:** ``POST /search`` with a JSON body; paginated via ``page`` /
  ``totalPages`` (paging block at top level of response).  Rows are under
  ``data.rows``.

* **Detail + download:** ``GET /details/{id}`` → ``sections[*].elements``
  where elements of ``type == "keyvalue"`` and ``value.type == "link"`` carry
  the public Azure-blob URL for each attached document.

No auth, no WAF, standard JSON headers.
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

_BASE = "https://weappegressprod.azurewebsites.net"
# Per-request header so /details carries ENGLISH category labels ("Own shares", not
# "Egne aktier"). Passed per-call (NOT on the shared session) so other backends in the
# same acquire() run keep their own language.
_EN_HEADERS = {"Accept-Language": "en"}
_BLOB_HOST = "https://saegressprod.blob.core.windows.net"
_MAX_PAGES = 100
_PAGE_SIZE = 100

# ---------------------------------------------------------------------------
# doc_type mapping (case-insensitive, from CategoryColumn key)
# ---------------------------------------------------------------------------

_CATEGORY_MAP: dict[str, str] = {
    # API category keys (CategoryColumn — present in older responses)
    "yearlyfinancialreport": "annual_report",
    "halfyearlyfinancialreport": "half_year_report",
    "quarterlyfinancialreport": "interim_statement",
    "interimstatement": "interim_statement",
    "insideinformation": "inside_information",
    "shareholder": "holding_notification",
    "totalvotingrightsandsharecapital": "holding_notification",
    "prospectus": "prospectus",
    "ownshares": "other",
    "paymentstogovernments": "other",
    "homememberstate": "other",
    "changeinrightsattachedtosecurities": "other",
    "relatedpartytransactions": "other",
    "takeoverbid": "other",
    "shortselling": "other",
    # Human-readable English labels (as exposed in /details — the live path: the
    # search row's CategoryColumn is "Udsteder"/issuer, so the real type lives here).
    "annualfinancialreport": "annual_report",
    "interimreport": "interim_statement",
    "quarterlyreport": "interim_statement",
    "majorshareholderannouncement": "holding_notification",
    "majorshareholder": "holding_notification",
    "totalvotingrightsandcapital": "holding_notification",
    "managerstransactions": "holding_notification",
    "acquisitionordisposalofownshares": "other",
}


def _doc_type(category: str) -> str:
    """Map an API category KEY or a human-readable category LABEL to a DOC_TYPES
    member. Normalises by stripping all non-alphanumerics so 'Half-yearly financial
    report' and 'HalfYearlyFinancialReport' collapse to the same key."""
    key = re.sub(r"[^a-z0-9]", "", (category or "").lower())
    return _CATEGORY_MAP.get(key, "other")


def _category_from_detail(detail: dict) -> str:
    """The human-readable category from a /details response.

    The first section ("Notification") lists, among its keyvalue text elements, the
    document category as an unnamed element (e.g. "Annual financial report") right
    after the "Type" element. Return the first text value that maps to a known
    doc_type, else "".
    """
    for section in (detail or {}).get("sections", [])[:1] or (detail or {}).get("sections", []):
        for elem in section.get("elements", []):
            if elem.get("type") != "keyvalue":
                continue
            value = elem.get("value") or {}
            if value.get("type") != "text":
                continue
            text = (value.get("text") or value.get("value") or "").strip()
            if text and _doc_type(text) != "other":
                return text
    return ""


# ---------------------------------------------------------------------------
# Name normalisation for CVR resolution
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Lowercase, strip diacritics, collapse whitespace, remove trailing ' a/s'."""
    t = (text or "").strip().casefold()
    # Strip diacritics
    t = "".join(
        c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)
    )
    # Collapse whitespace
    t = " ".join(t.split())
    # Strip trailing " a/s"
    if t.endswith(" a/s"):
        t = t[:-4].rstrip()
    return t


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class OamDK(OamSource):
    """Denmark OAM backend — Finanstilsynet OAM Publication JSON API.

    Resolves issuer name -> CVR via the public ``/config`` issuer list
    (exact normalised match, no fuzzy fallback).  Searches via ``POST /search``
    paginated, fetches ``GET /details/{id}`` for each row to obtain the
    Azure-blob document URLs.
    """

    name = "oam-dk"
    country = "DK"

    def __init__(self, fetcher=None, config=None):
        super().__init__(fetcher=fetcher, config=config)
        self._cvr_map: dict[str, list[str]] | None = None  # normalised_name -> [cvr, ...]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self, entity: Entity) -> list[Document]:
        """Return all regulated disclosures for *entity* from Finanstilsynet.

        Resolves the entity name to a CVR number via ``/config``, then
        paginates ``POST /search``, and GETs ``/details/{id}`` for each row
        to collect the Azure-blob document URLs.  Rows with no link elements
        in their detail are silently skipped (no error recorded).
        """
        cvr = self._resolve_cvr(entity.name)
        if cvr is None:
            return []

        now = datetime.now(timezone.utc).isoformat()
        docs: list[Document] = []
        page = 1

        while True:
            body = {
                "query": "",
                "filters": [
                    {
                        "type": "dropdown",
                        "key": "IssuerFilter",
                        "options": [cvr],
                    }
                ],
                "page": page,
                "pageSize": _PAGE_SIZE,
                "sorting": {
                    "key": "PublicationDateColumn",
                    "direction": "descending",
                },
            }
            try:
                resp = self.fetcher.post_json(f"{_BASE}/search", body, headers=_EN_HEADERS)
            except Exception as exc:  # noqa: BLE001
                self._record_error("search", f"{_BASE}/search", exc)
                break

            paging = resp.get("paging") or {}
            data_block = resp.get("data") or {}
            rows = data_block.get("rows") or []
            _tp_raw = paging.get("totalPages")
            total_pages = int(_tp_raw) if _tp_raw is not None else None
            # When totalPages is absent, pagination is driven by empty pages
            # (mirrors FI/IT pattern) rather than a possibly-missing count.

            if page == 1 and total_pages is not None and total_pages > _MAX_PAGES:
                self._record_error(
                    "truncated",
                    f"{_BASE}/search",
                    RuntimeError(
                        f"totalPages={total_pages} exceeds _MAX_PAGES={_MAX_PAGES}; "
                        "results truncated"
                    ),
                )

            for row in rows:
                row_id = str(row.get("id") or "")
                if not row_id:
                    continue
                category = row.get("CategoryColumn") or ""
                published_ts = row.get("PublicationDateColumn")
                headline = row.get("HeadlineColumn")

                # Fetch detail to get the blob URLs
                try:
                    detail = self.fetcher.get_json(f"{_BASE}/details/{row_id}", headers=_EN_HEADERS)
                except Exception as exc:  # noqa: BLE001
                    self._record_error("details", f"{_BASE}/details/{row_id}", exc)
                    continue

                files = _extract_files(detail)
                if not files:
                    # Row has no downloadable attachments — skip silently
                    continue

                # The live search row's CategoryColumn is "Udsteder" (issuer type), so
                # derive the real doc category from the detail's English label; fall back
                # to CategoryColumn (older responses carry the API key there).
                category = _category_from_detail(detail) or category

                docs.append(Document(
                    doc_id=f"dk-{row_id}",
                    lei=entity.lei,
                    country="DK",
                    doc_type=_doc_type(category),
                    period_end=None,
                    published_ts=published_ts,
                    discovered_ts=now,
                    language=None,
                    source=self.name,
                    files=files,
                    native_meta={
                        "headline": headline,
                        "issuer": row.get("IssuerColumn"),
                        "category": category,
                        "cvr": cvr,
                    },
                ))

            if not rows:
                break  # empty page — all docs fetched (works with or without totalPages)
            if total_pages is not None and page >= min(total_pages, _MAX_PAGES):
                break
            elif total_pages is None and page >= _MAX_PAGES:
                break  # safety backstop when server omits totalPages
            page += 1

        return docs

    # ------------------------------------------------------------------
    # CVR resolution
    # ------------------------------------------------------------------

    def _resolve_cvr(self, name: str) -> str | None:
        """Resolve an entity name to its CVR number via exact normalised match.

        Returns None (and records an error) when there is no unique match.
        """
        if not name:
            self._record_error(
                "resolve-name",
                f"{_BASE}/config",
                RuntimeError("entity name is empty; cannot resolve CVR"),
            )
            return None

        key = _normalise(name)

        if self._cvr_map is None:
            try:
                config = self.fetcher.get_json(f"{_BASE}/config", headers=_EN_HEADERS)
            except Exception as exc:  # noqa: BLE001
                self._record_error("config", f"{_BASE}/config", exc)
                return None
            self._cvr_map = _build_cvr_map(config)

        matches = self._cvr_map.get(key) or []
        if len(matches) == 1:
            return matches[0]

        self._record_error(
            "resolve-name",
            f"{_BASE}/config",
            RuntimeError(
                f"no unique CVR for name {name!r} "
                f"(normalised {key!r}); matches={matches}"
            ),
        )
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_cvr_map(config: dict) -> dict[str, list[str]]:
    """Build a normalised_name -> [cvr, ...] map from the /config response."""
    cache: dict[str, list[str]] = {}
    try:
        filters = (config or {}).get("components", {}).get("filters", [])
        for f in filters:
            if f.get("key") == "IssuerFilter":
                for opt in f.get("options", []):
                    cvr = str(opt.get("id") or "").strip()
                    label = opt.get("label") or ""
                    norm = _normalise(label)
                    if cvr and norm:
                        cache.setdefault(norm, []).append(cvr)
    except Exception:  # noqa: BLE001
        pass
    return cache


def _extract_files(detail: dict) -> list[dict]:
    """Extract all link-type elements from a /details response as file dicts."""
    files: list[dict] = []
    for section in (detail or {}).get("sections", []):
        for elem in section.get("elements", []):
            if elem.get("type") != "keyvalue":
                continue
            value = elem.get("value") or {}
            if value.get("type") != "link":
                continue
            url = (value.get("url") or "").strip()
            if not url:
                continue
            ext = os.path.splitext(url.split("?")[0])[1].lower()
            kind = "esef" if ext in (".zip", ".xhtml") else "document"
            name = os.path.basename(url.split("?")[0])
            files.append({"name": name, "kind": kind, "url": url})
    return files
