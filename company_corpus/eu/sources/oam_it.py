"""1Info (CONSOB OAM / Computershare) backend — Italy.

Unauthenticated JSON REST API at ``https://consob.1info.it/PORTALE1INFO``.

* ``GET  /API/companies/documenti``  → issuer name → ndg map.
* ``POST /API/Documenti``            → regulatory documents (DataTables response).
* ``POST /API/Comunicati``           → press-release-style comunicati.
* Download via ``PdfShow.aspx``      — ESEF zip uses ``protocolCodeXbrl``;
  PDF uses ``pdf`` id.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

BASE = "https://consob.1info.it/PORTALE1INFO"
# Downloads are served from the site ROOT (not under /PORTALE1INFO). Verified live:
# the /PORTALE1INFO/PdfViewer path returns a 404 HTML page, while the root path
# returns the real bytes (PDF %PDF- / ESEF PK.. zip). Keep these two hosts distinct.
# CONSOB's public PDF viewer takes a fixed, documented technical login (oneinfo/oneinfo); it is not a secret — pre-triaged for secret scanners.
_DL = (
    "https://consob.1info.it/PdfViewer/PdfShow.aspx"
    "?username=oneinfo&password=oneinfo&service=&type={filetype}&year={year}&file={file}&download=1"
)

# categoria → DOC_TYPES member
_CAT_MAP: dict[str, str] = {
    "1.1": "annual_report",
    "1.2": "half_year_report",
    "2.2": "inside_information",
    "2.3": "holding_notification",
}

# Extensions that ARE plain documents — a protocolCodeXbrl with one of these is not
# an ESEF package and must NOT get zip treatment.
_NON_DOC_EXTS = {".pdf", ".html", ".xhtml"}

# DataTables page size + a safety backstop on per-issuer pagination (well above any
# real issuer's filing count; ENI — among the largest — has ~1.5k comunicati).
_PAGE = 500
_MAX_RECORDS = 50_000

_WS_RE = re.compile(r"\s+")


def _normalise(name: str) -> str:
    """Collapse whitespace and uppercase — matches stray-spaced descrizioni."""
    return _WS_RE.sub(" ", name).strip().upper()


def _year_utc(epoch_seconds: int | float | None) -> str | None:
    """Return the four-digit UTC year as a string from an epoch-seconds integer."""
    if epoch_seconds is None:
        return None
    try:
        return str(datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).year)
    except (OSError, ValueError, OverflowError):
        return None


def _iso_utc(epoch_seconds: int | float | None) -> str | None:
    """Return an ISO 8601 timestamp string from epoch-seconds."""
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


class OneInfoIT(OamSource):
    """Italy OAM backend — resolves issuer name → ndg, then POSTs for docs."""

    name = "oam-it"
    country = "IT"

    def __init__(self, fetcher=None, config=None):
        super().__init__(fetcher=fetcher, config=config)
        self._companies: dict[str, int] | None = None  # normalised_name -> ndg

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self, entity: Entity) -> list[Document]:
        ndg = self._resolve_ndg(entity.name)
        if ndg is None:
            return []

        now = datetime.now(timezone.utc).isoformat()
        out: list[Document] = []

        for endpoint, filetype in [
            ("/API/Documenti", "documenti"),
            ("/API/Comunicati", "comunicati"),
        ]:
            out.extend(
                self._discover_endpoint(BASE + endpoint, filetype, ndg, entity, now)
            )

        return out

    def _discover_endpoint(
        self, url: str, filetype: str, ndg: int, entity: Entity, now: str
    ) -> list[Document]:
        """Page through one DataTables endpoint until every record is fetched.

        A single ``length``-capped request silently drops everything past the first
        page (ENI has ~660 documenti / ~1550 comunicati), so we walk ``start`` until
        we have consumed ``recordsFiltered`` rows — never silently partial.
        """
        docs: list[Document] = []
        start = 0
        total: int | None = None
        while True:
            body = {
                "draw": 1,
                "start": start,
                "length": _PAGE,
                "SearchFilter": {"emittente": [str(ndg)]},
            }
            try:
                resp = self.fetcher.post_json(url, body)
            except Exception as exc:  # noqa: BLE001
                self._record_error("discover", f"{url}?start={start}", exc)
                break

            rows = resp.get("data") or []
            if total is None:
                total = resp.get("recordsFiltered")
            for row in rows:
                doc = self._build_document(row, filetype, entity, now)
                if doc is not None:
                    docs.append(doc)

            start += len(rows)
            if not rows or (total is not None and start >= total):
                break
            if start >= _MAX_RECORDS:
                self._record_error(
                    "discover",
                    f"{url}?start={start}",
                    RuntimeError(
                        f"pagination hit the {_MAX_RECORDS}-record cap "
                        f"(recordsFiltered={total}); results truncated"
                    ),
                )
                break

        return docs

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_companies(self) -> dict[str, int] | None:
        """Load the name->ndg map.  Returns None (not {}) on failure so the caller
        knows not to cache the result — a transient error must not permanently silence
        every subsequent entity."""
        url = BASE + "/API/companies/documenti"
        try:
            rows = self.fetcher.get_json(url)
            return {
                _normalise(r["descrizione"]): r["ndg"]
                for r in rows
                if r.get("descrizione") and r.get("ndg") is not None
            }
        except Exception as exc:  # noqa: BLE001
            self._record_error("companies", url, exc)
            return None  # do NOT return {}; caller must not cache this

    def _resolve_ndg(self, name: str) -> int | None:
        if self._companies is None:
            loaded = self._load_companies()
            if loaded is None:
                # Load failed; error already recorded. Don't cache the failure so
                # a subsequent call can retry (e.g. after a transient network error).
                return None
            self._companies = loaded

        key = _normalise(name)
        ndg = self._companies.get(key)
        if ndg is None:
            self._record_error(
                "resolve-no-match",
                BASE + "/API/companies/documenti",
                RuntimeError(
                    f"no company match for {name!r} "
                    f"(normalised {key!r}) in 1Info company list"
                ),
            )
        return ndg

    def _build_document(
        self,
        row: dict,
        filetype: str,
        entity: Entity,
        now: str,
    ) -> Document | None:
        """Build a Document from a single data row; return None if no files."""
        pdf = row.get("pdf")
        xbrl = row.get("protocolCodeXbrl")
        storage_epoch = row.get("dataStoccaggio")
        exercise_epoch = row.get("dataEsercizio")

        files: list[dict] = []

        # ESEF zip — only when protocolCodeXbrl is present and NOT a plain doc ext
        if xbrl:
            ext = ""
            if "." in xbrl:
                ext = xbrl[xbrl.rfind("."):]
            if ext.lower() not in _NON_DOC_EXTS:
                year = _year_utc(exercise_epoch) or _year_utc(storage_epoch) or "0"
                url = _DL.format(filetype=filetype, year=year, file=quote(xbrl, safe=''))
                files.append({"name": xbrl + ".zip", "kind": "esef", "url": url})

        # Regular PDF
        if pdf:
            year = _year_utc(storage_epoch) or "0"
            pdf_name = pdf + ".pdf"
            url = _DL.format(filetype=filetype, year=year, file=quote(pdf_name, safe=''))
            files.append({"name": pdf_name, "kind": "document", "url": url})

        if not files:
            return None

        categoria = row.get("categoria") or ""
        doc_type = _CAT_MAP.get(categoria, "other")

        # doc_id: prefer pdf id, fall back to protocolCode
        raw_id = pdf or row.get("protocolCode") or row.get("id")
        doc_id = f"it-{filetype}-{raw_id}"

        return Document(
            doc_id=doc_id,
            lei=entity.lei,
            country="IT",
            doc_type=doc_type,
            period_end=None,
            published_ts=_iso_utc(storage_epoch),
            discovered_ts=now,
            language="it",
            source=self.name,
            files=files,
            native_meta=row,
        )
