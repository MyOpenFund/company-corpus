"""AFM (Autoriteit Financiële Markten) backend — Netherlands.

The AFM is the Dutch OAM. Financial-reporting register flow:

1. **Session bootstrap (politeness/cookie):** GET the register landing page on
   the shared ``requests.Session`` — drops a session cookie.
2. **Bulk export:** GET the ``export.aspx`` endpoint → a single ``<register>``
   XML blob (~19k ``<vermelding>`` entries, each entry containing a NESTED
   ``<vermelding>`` label that must not be confused with the outer entry block).
   Outer-entry fields: ``<id>``, ``<datum>``, ``<uitgevende-instelling>``,
   ``<boekjaar>``, ``<filename>``, ``<objecttype_eng>``.
3. **Filter to target issuer:** exact normalised match on
   ``<uitgevende-instelling>`` vs ``entity.name``. Normalise: collapse
   whitespace, casefold, strip diacritics (NFKD), strip trailing Dutch
   legal-form suffix (n.v., b.v.). Never substring/prefix.
4. **Resolve download URL per entry:** GET the details page
   (``details?id=<id>``), find the ``downloadregisterfile.aspx?…enc=…`` link.
   On failure → index-only Document with ``capture_failed=True`` (no url).

Every network step is wrapped; one failure never aborts the rest.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_BASE = "https://www.afm.nl"
_REGISTER_PAGE = (
    _BASE + "/en/sector/registers/meldingenregisters/financiele-verslaggeving"
)
_EXPORT_URL = (
    _BASE + "/export.aspx?type=e8825b05-4004-4301-b736-651e8c61053d&format=xml"
)
_DETAILS_URL = (
    _BASE + "/en/sector/registers/meldingenregisters/financiele-verslaggeving/details?id={id}"
)
_REGISTER_NAME = "financiele-verslaggeving"

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# Each register entry is an outer <vermelding> that also contains ONE inner
# <vermelding>…</vermelding> display label. This non-greedy match stops at the
# FIRST (inner) </vermelding> — which is safe because all of the named field
# tags (id, datum, uitgevende-instelling, boekjaar, filename, objecttype_eng)
# appear BEFORE the inner label, so the captured chunk holds every field. We
# then pull each field out of the chunk by its own tag regex below.
_OUTER_VERMELDING_RE = re.compile(
    r"<vermelding>(.*?)</vermelding>",
    re.S,
)

# Field extractors — each one captures the text content of a known tag.
_ID_RE = re.compile(r"<id>(.*?)</id>", re.S)
_DATUM_RE = re.compile(r"<datum>(.*?)</datum>", re.S)
_INSTELLING_RE = re.compile(r"<uitgevende-instelling>(.*?)</uitgevende-instelling>", re.S)
_BOEKJAAR_RE = re.compile(r"<boekjaar>(.*?)</boekjaar>", re.S)
_FILENAME_RE = re.compile(r"<filename>(.*?)</filename>", re.S)
_OBJECTTYPE_ENG_RE = re.compile(r"<objecttype_eng>(.*?)</objecttype_eng>", re.S)

# downloadregisterfile href in the details page.
_ENC_HREF_RE = re.compile(
    r'href="(/downloadregisterfile\.aspx\?[^"]*enc=[^"]+)"',
    re.I,
)

# Whitespace collapse.
_WS_RE = re.compile(r"\s+")

# Trailing Dutch legal-form suffix.
_LEGAL_SUFFIX_RE = re.compile(r",?\s*(?:n\.v\.|b\.v\.)$", re.I)

# ---------------------------------------------------------------------------
# Doc-type mapping
# ---------------------------------------------------------------------------

_DOC_TYPE_MAP: dict[str, str] = {
    "Annual financial report": "annual_report",
    "Half-yearly financial report": "half_year_report",
}

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _normalise(name: str) -> str:
    """Collapse whitespace, casefold, strip diacritics (NFKD), strip trailing
    Dutch legal-form suffix (n.v., b.v.)."""
    n = _WS_RE.sub(" ", name).strip().casefold()
    n = "".join(
        c for c in unicodedata.normalize("NFKD", n) if not unicodedata.combining(c)
    )
    n = _LEGAL_SUFFIX_RE.sub("", n).strip()
    return n


def _parse_datum(datum: str) -> str | None:
    """Parse AFM datum format M/D/YYYY h:mm:ss AM/PM -> ISO date YYYY-MM-DD.

    Returns None on any parse failure.
    """
    try:
        dt = datetime.strptime(datum.strip(), "%m/%d/%Y %I:%M:%S %p")
        return dt.date().isoformat()
    except (ValueError, AttributeError):
        return None


def _first(pattern: re.Pattern, text: str) -> str:
    """Return the first capture group of a pattern match, or empty string."""
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class AfmNL(OamSource):
    """Netherlands OAM backend — AFM bulk financial-reporting register export.

    Pulls the AFM's bulk XML export (~19k entries), filters to the target
    issuer by ``<uitgevende-instelling>`` (exact, diacritic/legal-suffix
    folded, no-guess), maps objecttype_eng to doc_type, and resolves each
    entry's stateless ``downloadregisterfile.aspx?enc=`` URL via a per-doc
    details hop (index-only when a hop fails).
    """

    name = "oam-nl"
    country = "NL"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self, entity: Entity) -> list[Document]:
        if not entity.name:
            return []

        # 1. Bootstrap session (politeness/cookie); tolerate failure.
        try:
            self.fetcher.get_text(_REGISTER_PAGE)
        except Exception:  # noqa: BLE001
            pass  # non-fatal: the export itself is stateless

        # 2. Fetch the bulk XML export.
        try:
            xml = self.fetcher.get_text(_EXPORT_URL)
        except Exception as exc:  # noqa: BLE001
            self._record_error("export", _EXPORT_URL, exc)
            return []

        # 3. Parse entries and filter to the target issuer.
        norm_target = _normalise(entity.name)
        now = datetime.now(timezone.utc).isoformat()
        out: list[Document] = []

        for m in _OUTER_VERMELDING_RE.finditer(xml):
            block = m.group(1)

            instelling = _first(_INSTELLING_RE, block)
            if not instelling:
                continue
            if _normalise(instelling) != norm_target:
                continue  # not this issuer — correct filter, not a drop

            # 4. Extract entry fields.
            entry_id = _first(_ID_RE, block)
            datum = _first(_DATUM_RE, block)
            boekjaar = _first(_BOEKJAAR_RE, block)
            filename = _first(_FILENAME_RE, block)
            objecttype_eng = _first(_OBJECTTYPE_ENG_RE, block)

            published_ts = _parse_datum(datum)
            doc_type = _DOC_TYPE_MAP.get(objecttype_eng, "other")
            doc_id = f"nl-{entry_id}"

            # 5. Resolve download URL via per-doc details hop.
            file_entry = self._resolve_file(entry_id, filename)

            out.append(Document(
                doc_id=doc_id,
                lei=entity.lei,
                country="NL",
                doc_type=doc_type,
                period_end=None,
                published_ts=published_ts,
                discovered_ts=now,
                language="nl",
                source=self.name,
                files=[file_entry],
                native_meta={
                    "boekjaar": boekjaar,
                    "filename": filename,
                    "register": _REGISTER_NAME,
                },
            ))

        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_file(self, entry_id: str, filename: str) -> dict:
        """GET the details page and extract the downloadregisterfile enc= URL.

        On failure (network error or enc link absent) returns an index-only
        file entry with ``capture_failed=True`` and no ``url`` key, exactly
        mirroring the oam_de.py capture-failure pattern.
        """
        details_url = _DETAILS_URL.format(id=entry_id)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        kind = "esef" if ext in ("xbri", "zip") else "document"

        try:
            html = self.fetcher.get_text(details_url)
        except Exception as exc:  # noqa: BLE001
            self._record_error("details", details_url, exc)
            return {"name": filename, "kind": kind, "capture_failed": True}

        enc_m = _ENC_HREF_RE.search(html)
        if not enc_m:
            self._record_error(
                "details",
                details_url,
                RuntimeError(f"no downloadregisterfile enc= link found for id={entry_id}"),
            )
            return {"name": filename, "kind": kind, "capture_failed": True}

        file_url = _BASE + enc_m.group(1).replace("&amp;", "&")
        return {"name": filename, "kind": kind, "url": file_url}
