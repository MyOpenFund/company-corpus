"""filings.xbrl.org backend — complementary structured ESEF annual reports.

Free XBRL International aggregator (JSON:API). NOT a census (DE/IE missing, IT
partial); used to enrich, never as the sole source. One filing -> one
annual_report Document. Download URLs in the API are paths relative to BASE.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import requests

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource


_PAGE = 100  # JSON:API page size; an issuer's ESEF reports are far under this.


class FilingsXbrlOrg(OamSource):
    name = "filings.xbrl.org"
    country = "EU"
    BASE = "https://filings.xbrl.org"

    def discover(self, entity: Entity) -> list[Document]:
        if not entity.lei:
            return []
        # Recon-confirmed: filter[entity_api_id] is INVALID (400); the working query
        # is the entity's own filings collection. Many entities return [] or 404
        # (erratic coverage) -> both are "no filings", never an abort. A 404 is
        # the aggregator's definitive "not indexed": a NOTE on this backend's
        # row, not an error (an error would degrade an eu-acquire run whose
        # national OAM is healthy and empty). Anything else is a dead source.
        url = f"{self.BASE}/api/entities/{entity.lei}/filings?page[size]={_PAGE}"
        try:
            rows = self.fetcher.get_json(url).get("data") or []
        except Exception as exc:  # noqa: BLE001
            if _is_http_404(exc):
                self._record_note("not-indexed", url,
                                  "HTTP 404: issuer not indexed by filings.xbrl.org")
            else:
                self._record_error("discover", url, exc)
            return []
        # Single page (an issuer's ESEF reports are a handful — far under the cap).
        # Still never silently partial: a full page means there may be more.
        if len(rows) >= _PAGE:
            self._record_error("truncated", url,
                               f"{len(rows)} filings at the {_PAGE}-page cap; more may exist")
        now = datetime.now(timezone.utc).isoformat()
        out: list[Document] = []
        for row in rows:
            a = row.get("attributes", {})
            files = [{"name": (a.get(k) or "").rsplit("/", 1)[-1],
                      "url": self.BASE + a[k], "kind": k}
                     for k in ("package_url", "report_url", "json_url") if a.get(k)]
            out.append(Document(
                doc_id=f"fxo-{row.get('id')}", lei=entity.lei, country=a.get("country", entity.country),
                doc_type="annual_report", period_end=_to_date(a.get("period_end")),
                published_ts=a.get("date_added"), discovered_ts=now, language=None,
                source=self.name,
                files=[dict(f, sha256=a.get("sha256") if f["kind"] == "package_url" else None) for f in files],
                native_meta=a))
        return out


def _is_http_404(exc: Exception) -> bool:
    """A ``requests.HTTPError`` whose response is a 404 — the only shape the
    live fetcher produces for "not indexed"; any other failure is a dead source."""
    if not isinstance(exc, requests.HTTPError):
        return False
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def _to_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None
