"""SEC XBRL company-facts source (structured financials, family F1).

Fetches ``https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`` and
turns it into one curated :class:`~company_corpus.financials.PeriodSummary` per
reporting period. Point-in-time issuer naming reuses the submissions API's
``formerNames`` (company facts only carry the current name).
"""

from __future__ import annotations

from collections.abc import Sequence

from ..config import normalize_cik
from ..financials import PeriodSummary, attach_ttm_metrics, build_period_summaries
from ..naming import name_as_of, parse_former_names
from .base import Source
from .edgar_submissions import SUBMISSIONS_URL

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


class EdgarXBRL(Source):
    """Yield per-period financial summaries for a CIK from XBRL company facts."""

    name = "edgar_xbrl"

    def companyfacts(self, cik: str) -> dict | None:
        url = COMPANYFACTS_URL.format(cik=normalize_cik(cik))
        try:
            return self.fetcher.get_json(url)
        except Exception as exc:  # noqa: BLE001 - record and skip this CIK
            self._record_error("companyfacts", url, exc)
            return None

    def _former_names(self, cik: str):
        """Best-effort formerNames from the submissions API (for point-in-time names)."""
        url = SUBMISSIONS_URL.format(cik=normalize_cik(cik))
        try:
            data = self.fetcher.get_json(url)
        except Exception:  # noqa: BLE001 - non-fatal; fall back to current name
            return ""
        return data

    def period_summaries(
        self, cik: str, *, since_year: int | None = None, until_year: int | None = None
    ) -> tuple[dict | None, list[PeriodSummary]]:
        """Return ``(raw_companyfacts, [PeriodSummary, ...])`` for a CIK."""
        facts = self.companyfacts(cik)
        if not facts:
            return None, []

        current = facts.get("entityName", "")
        subs = self._former_names(cik)
        former = parse_former_names(subs.get("formerNames")) if isinstance(subs, dict) else []
        sic = subs.get("sic") if isinstance(subs, dict) else None

        def name_for_date(d):
            return name_as_of(d, current, former)

        summaries = build_period_summaries(
            facts, company=current, company_current=current,
            name_for_date=name_for_date, since_year=since_year, until_year=until_year,
            sic=sic,
        )
        attach_ttm_metrics(facts, summaries)
        return facts, summaries

    # Uniform Source surface (kept for symmetry with other sources).
    def discover(
        self, cik: str, *, since_year: int | None = None, until_year: int | None = None
    ) -> Sequence[PeriodSummary]:
        return self.period_summaries(cik, since_year=since_year, until_year=until_year)[1]
