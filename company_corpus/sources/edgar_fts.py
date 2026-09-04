"""Reverse CUSIP -> CIK resolution via EDGAR full-text search (EFTS).

Given a bond's CUSIP, find the SEC filing that lists it and take that filer's
CIK. The search is **restricted to issuer offering forms** (424B / FWP / S-3):
an unrestricted CUSIP search ranks fund *holders* (N-PORT / 13F) first, not the
issuer, so the restriction is what makes the top hit trustworthy.

Network unit: one GET per CUSIP, fair-access throttled via the shared Fetcher.
``edgar_fts`` is an anticipated provenance value in ``models.py``.
"""

from __future__ import annotations

from urllib.parse import quote

from ..config import normalize_cik
from .base import Source

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
# Issuer offering forms only (no 13F/N-PORT holdings reports). NOTE: EFTS rejects
# amendment tokens with a slash (e.g. "S-3/A") -- including one makes the query
# return zero hits -- so only base form codes appear here. All chars are
# URL-safe (alphanumerics, hyphens, commas), so no encoding is needed.
OFFERING_FORMS = "424B1,424B2,424B3,424B4,424B5,424B6,424B7,424B8,FWP,S-3,S-3ASR"


class EdgarFTS(Source):
    """Resolve a CUSIP to its issuer's CIK via EFTS, restricted to offering forms."""

    name = "edgar_fts"

    def resolve(self, cusip: str) -> tuple[str, str] | None:
        """Return ``(cik, display_name)`` for the top offering-form filing that
        mentions ``cusip``, or ``None`` (no hit / no cik / fetch error)."""
        # quote the CUSIP defensively (callers pass alnum CUSIPs, but resolve() is
        # public); OFFERING_FORMS is already all-URL-safe (no slash tokens).
        url = f'{EFTS_URL}?q=%22{quote(cusip, safe="")}%22&forms={OFFERING_FORMS}'
        try:
            data = self.fetcher.get_json(url)
        except Exception as exc:  # noqa: BLE001 - record and skip a bad lookup
            self._record_error("fts", url, exc)
            return None
        hits = (data.get("hits") or {}).get("hits") or []
        if not hits:
            return None
        source = hits[0].get("_source") or {}
        ciks = source.get("ciks") or []
        if not ciks:
            return None
        try:
            cik = normalize_cik(ciks[0])
        except ValueError:
            return None
        names = source.get("display_names") or []
        return cik, (names[0] if names else "")
