"""FCA National Storage Mechanism (NSM) backend — United Kingdom.

The NSM is an Elasticsearch-backed JSON API exposing 5.3M regulated disclosures.
Identity is via exact LEI — the cleanest resolution of all backends (no name fuzzing).

* **Search:** POST ``https://api.data.fca.org.uk/search?index=fca-nsm-searchdata``
  with a custom envelope body; response is a standard ES hits envelope.
* **Download:** ``https://data.fca.org.uk/artefacts/<download_link>`` — stateless GET,
  no auth.  ``.pdf`` → PDF; ``.html`` → RNS announcement; ``.zip``/``.xhtml`` → ESEF.
* **Pagination:** ``from``/``size``; ES deep-paging cap at ``from+size <= 10000``.
  Truncation at ``_MAX_RESULTS`` is visible (recorded as an error, never silent).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

_SEARCH_URL = "https://api.data.fca.org.uk/search?index=fca-nsm-searchdata"
_ARTEFACTS_BASE = "https://data.fca.org.uk/artefacts/"
_PAGE = 100
_MAX_RESULTS = 10_000

# ---------------------------------------------------------------------------
# doc_type mapping
# ---------------------------------------------------------------------------

# Order matters: earlier rules take priority.
_TYPE_RULES: list[tuple[str, str]] = [
    # Exact / prefix matches first (most specific)
    ("Annual Financial Report", "annual_report"),
    ("Half-yearly Report", "half_year_report"),
    ("Half Yearly", "half_year_report"),
    ("Interim Results", "half_year_report"),
    ("Interim Management Statement", "interim_statement"),
    # Contains-based
    ("Prospectus", "prospectus"),
    # Shareholding / voting notifications
    ("Total Voting Rights", "holding_notification"),
    ("Holding(s) in Company", "holding_notification"),
    ("Form 8.3", "holding_notification"),
    ("Form 8.5", "holding_notification"),
    ("Shareholding", "holding_notification"),
    # Inside information
    ("Inside Information", "inside_information"),
    # Governance / corporate actions
    ("Notice of", "governance"),
    ("Result of AGM", "governance"),
    ("Directorate", "governance"),
    ("Proxy Form", "governance"),
    ("Meeting", "governance"),
    ("Board", "governance"),
]


def _doc_type(type_str: str) -> str:
    """Map an NSM ``type`` field to a ``DOC_TYPES`` member (case-insensitive)."""
    t = (type_str or "").strip()
    t_lower = t.lower()
    for keyword, mapped in _TYPE_RULES:
        if keyword.lower() in t_lower:
            return mapped
    return "other"


def _file_kind(download_link: str, tag_esef: str) -> str:
    """Derive the file kind from the download_link extension and tag_esef flag."""
    ext = os.path.splitext(download_link)[1].lower()
    if ext in (".zip", ".xhtml") or tag_esef:
        return "esef"
    if ext == ".pdf":
        return "document"
    return "announcement"


class NsmGB(OamSource):
    """United Kingdom OAM backend — FCA National Storage Mechanism (NSM).

    Queries the NSM Elasticsearch API filtered by exact LEI.  No fuzzy name
    fallback: if the entity has no LEI, ``discover`` returns an empty list.
    """

    name = "oam-gb"
    # Home/source jurisdiction of the mechanism (the FCA NSM is in the UK). Each
    # Document is labelled with the *issuer's* country (entity.country); this
    # constant is only the provenance fallback when that is unknown — it is NOT
    # used for dispatch (that goes through COUNTRY_BACKENDS) or per-doc labelling.
    country = "GB"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self, entity: Entity) -> list[Document]:
        """Return all regulated disclosures for *entity* from the FCA NSM.

        Requires ``entity.lei`` — returns ``[]`` immediately if absent.
        Paginates the ES API in steps of ``_PAGE`` up to ``_MAX_RESULTS``.
        Truncation is recorded as an error (never silent).
        """
        if not entity.lei:
            return []

        now = datetime.now(timezone.utc).isoformat()
        docs: list[Document] = []
        from_offset = 0
        total: int | None = None

        while True:
            body = {
                "from": from_offset,
                "size": _PAGE,
                "sort": "publication_date",
                "sortorder": "desc",
                "criteriaObj": {
                    "criteria": [{"name": "lei", "value": entity.lei}],
                    "dateCriteria": None,
                },
            }
            try:
                resp = self.fetcher.post_json(_SEARCH_URL, body)
            except Exception as exc:  # noqa: BLE001
                self._record_error("search", _SEARCH_URL, exc)
                break

            hits_env = resp.get("hits") or {}

            # Read total on first response; record truncation immediately if needed.
            # The `total is None` gate runs exactly once, so this can't double-record.
            if total is None:
                total_raw = hits_env.get("total")
                if total_raw is not None:
                    total_obj = total_raw or {}
                    total = total_obj.get("value", 0) if isinstance(total_obj, dict) else int(total_obj)
                # When "total" is absent, pagination is driven by empty hits pages
                # (mirrors FI/IT pattern) rather than a possibly-missing count.
                if total is not None and total > _MAX_RESULTS:
                    self._record_error(
                        "truncated",
                        _SEARCH_URL,
                        RuntimeError(
                            f"total={total} exceeds ES deep-paging cap of {_MAX_RESULTS}; "
                            "results truncated"
                        ),
                    )

            hits = hits_env.get("hits") or []
            if not hits:
                break

            for hit in hits:
                src = hit.get("_source") or {}
                download_link = src.get("download_link") or ""
                if not download_link:
                    continue
                # Defence in depth: download_link is a server-issued relative NSM path
                # (e.g. "NSM/RNS/<uuid>.html"). Reject anything that could escape the
                # artefacts base — an absolute URL, a scheme, or a parent-dir traversal —
                # so the built URL can never point off-host.
                if (
                    "://" in download_link
                    or download_link.startswith("/")
                    or ".." in download_link
                ):
                    self._record_error("download-link", download_link,
                                       RuntimeError("unexpected/unsafe download_link; skipped"))
                    continue

                disclosure_id = src.get("disclosure_id") or src.get("seq_id") or ""
                doc_id = f"gb-{disclosure_id}" if disclosure_id else f"gb-{from_offset}-{len(docs)}"

                tag_esef = src.get("tag_esef") or ""
                kind = _file_kind(download_link, tag_esef)
                file_name = os.path.basename(download_link)

                doc = Document(
                    doc_id=doc_id,
                    # The NSM is also the de-facto OAM for Irish issuers (this
                    # backend is wired for IE too); label the document with the
                    # issuer's own country, while ``source`` keeps the NSM
                    # provenance. When the issuer country is unknown, fall back to
                    # the mechanism's own jurisdiction (provenance) — never a
                    # fabricated third country.
                    lei=entity.lei,
                    country=entity.country or self.country,
                    doc_type=_doc_type(src.get("type") or ""),
                    period_end=None,
                    published_ts=src.get("publication_date"),
                    discovered_ts=now,
                    language="en",
                    source=self.name,
                    files=[{
                        "name": file_name,
                        "kind": kind,
                        "url": _ARTEFACTS_BASE + download_link,
                    }],
                    native_meta={
                        "type": src.get("type"),
                        "headline": src.get("headline"),
                        "source": src.get("source"),
                        "isin": src.get("isin"),
                        "company": src.get("company"),
                        "tag_esef": tag_esef,
                    },
                )
                docs.append(doc)

            from_offset += _PAGE

            # ES deep-paging hard cap: do not request beyond _MAX_RESULTS.
            if from_offset >= _MAX_RESULTS:
                break

            if total is not None and from_offset >= total:
                break

        return docs
