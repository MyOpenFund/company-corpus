"""Keyless Virk Regnskaber acquisition — annual-report filings search + download.

Virk (Erhvervsstyrelsen) exposes an Elasticsearch endpoint at
``http://distribution.virk.dk/offentliggoerelser/_search`` that requires **no
authentication**.  Documents (bare XBRL / PDF) are fetched by GET from the URL
embedded in each filing's ``dokumenter`` list.

Gzip caveat
-----------
The Virk document server sends gzip-compressed payloads **without** setting the
``Content-Encoding: gzip`` response header.  This means the ``requests`` / HTTP
layer does **not** auto-decompress.  :func:`fetch_virk_document` checks the
first two bytes for the gzip magic (``\\x1f\\x8b``) and decompresses explicitly
when present.

Public API
----------
search_virk_filings(cvr, *, fetcher) -> list[dict]
    Returns the ``_source`` of every filing hit, sorted newest-first.
    Returns ``[]`` on any error (batch-safe, never raises).

fetch_virk_document(url, *, fetcher) -> bytes | None
    Downloads a Virk document; gunzips if magic bytes indicate gzip.
    Returns ``None`` on any error.

route_document(doc) -> "esef" | "fsa" | None
    Classifies a single ``dokument`` dict by ``dokumentType`` + mime type.
    ``AARSRAPPORT_ESEF`` + ``application/xml``  → ``"esef"``
    ``AARSRAPPORT``      + ``application/xml``  → ``"fsa"``
    Anything else (PDF, iXBRL viewer, management report …)  → ``None``.
"""

from __future__ import annotations

import gzip
import logging

log = logging.getLogger(__name__)

BASE = "http://distribution.virk.dk"
_SEARCH_URL = f"{BASE}/offentliggoerelser/_search"
_PAGE_SIZE = 50

_GZIP_MAGIC = b"\x1f\x8b"


def search_virk_filings(cvr: str, *, fetcher) -> list[dict]:
    """Search Virk for annual-report filings for *cvr* (8-digit string).

    Issues a POST to the Elasticsearch endpoint with a ``term`` query on
    ``cvrNummer`` (integer), sorted newest-first.  Returns the
    ``hits.hits[*]._source`` list; returns ``[]`` on any error.

    Parameters
    ----------
    cvr:
        8-digit CVR number as a string (e.g. ``"24256790"``).
    fetcher:
        A :class:`~company_corpus.http.Fetcher` instance (or any object
        exposing ``post_json(url, body)``).
    """
    body = {
        "query": {"term": {"cvrNummer": int(cvr)}},
        "size": _PAGE_SIZE,
        "sort": [{"offentliggoerelsesTidspunkt": {"order": "desc"}}],
    }
    try:
        response = fetcher.post_json(_SEARCH_URL, body)
        hits = response.get("hits", {}).get("hits", [])
        return [h["_source"] for h in hits if "_source" in h]
    except Exception:  # noqa: BLE001
        log.debug("Virk search failed for CVR %s", cvr, exc_info=True)
        return []


def fetch_virk_document(url: str, *, fetcher) -> bytes | None:
    """Download a Virk document and return its bytes.

    If the response body starts with the gzip magic ``\\x1f\\x8b`` the payload
    is decompressed before returning — the Virk server omits
    ``Content-Encoding: gzip`` so the HTTP layer will **not** auto-decompress.

    Parameters
    ----------
    url:
        Full document URL (from ``dokumenter[*].dokumentUrl``).
    fetcher:
        A :class:`~company_corpus.http.Fetcher` instance (or any object
        exposing ``get(url)`` that returns a response with a ``.content``
        attribute).

    Returns
    -------
    bytes or None
        Decompressed (or raw) document bytes, or ``None`` on any error.
    """
    try:
        resp = fetcher.get(url)
        raw: bytes = resp.content
    except Exception:  # noqa: BLE001
        log.debug("Virk document fetch failed for %s", url, exc_info=True)
        return None

    if raw[:2] == _GZIP_MAGIC:
        try:
            return gzip.decompress(raw)
        except Exception:  # noqa: BLE001
            log.debug("Virk gzip decompress failed for %s", url, exc_info=True)
            return None

    return raw


def route_document(doc: dict) -> str | None:
    """Classify a single Virk ``dokument`` dict (from ``dokumenter``).

    Decision table
    --------------
    ``dokumentType == "AARSRAPPORT_ESEF"``  AND  ``dokumentMimeType == "application/xml"``
        → ``"esef"``   (bare XBRL, Arelle/stdlib path A)

    ``dokumentType == "AARSRAPPORT"``       AND  ``dokumentMimeType == "application/xml"``
        → ``"fsa"``    (DK-GAAP FSA bare XBRL, path B)

    Anything else (PDF, ``application/xhtml+xml`` iXBRL viewer, management-
    review PDF, …)
        → ``None``     (skip)

    Parameters
    ----------
    doc:
        A single ``dokument`` dict from the filing ``_source["dokumenter"]``
        list, or a full ``_source`` dict whose ``dokumenter`` list you have
        already inspected.
    """
    dtype = doc.get("dokumentType", "")
    mime = doc.get("dokumentMimeType", "")

    if dtype == "AARSRAPPORT_ESEF" and mime == "application/xml":
        return "esef"
    if dtype == "AARSRAPPORT" and mime == "application/xml":
        return "fsa"
    return None
