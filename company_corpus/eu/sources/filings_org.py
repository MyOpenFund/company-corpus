"""filings.xbrl.org backend — complementary structured ESEF annual reports.

Free XBRL International aggregator (JSON:API). NOT a census (DE/IE missing, IT
partial); used to enrich, never as the sole source. One filing -> one
annual_report Document. Download URLs in the API are paths relative to BASE.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

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
        # (erratic coverage) -> treat both as "no filings", never an error abort.
        url = f"{self.BASE}/api/entities/{entity.lei}/filings?page[size]={_PAGE}"
        try:
            rows = self.fetcher.get_json(url).get("data") or []
        except Exception as exc:  # noqa: BLE001  (404 = not indexed -> no filings)
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


def _to_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None
