"""Exhaustive discovery via EDGAR quarterly full-index files.

``https://www.sec.gov/Archives/edgar/full-index/<year>/QTR<q>/master.idx`` lists
*every* disseminated filing in that quarter as pipe-delimited rows:

    CIK|Company Name|Form Type|Date Filed|Filename

This is the path to the *full universe* (any filer, 1993+). It is cheap to scan
(~33 MB/quarter) and complements the per-CIK submissions source used for the
curated tier. The index gives the complete-submission ``.txt`` path; the primary
document is resolved later at download time.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date

from ..config import normalize_cik
from ..models import FilingRecord
from ..taxonomy import FormType, from_edgar_form
from .base import Source

MASTER_IDX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{qtr}/master.idx"
ARCHIVES_BASE = "https://www.sec.gov/Archives/"


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:  # pragma: no cover - defensive
        return None


class EdgarFullIndex(Source):
    """Yield filings for a (year, quarter) from the master index."""

    name = "edgar_index"

    def discover(
        self,
        year: int,
        quarter: int,
        scope: Sequence[FormType] = (),
        ciks: set[str] | None = None,
    ) -> Iterator[FilingRecord]:
        scope_set = set(scope) if scope else None
        cik_filter = {normalize_cik(c) for c in ciks} if ciks else None
        url = MASTER_IDX_URL.format(year=year, qtr=quarter)
        try:
            text = self.fetcher.get_text(url)
        except Exception as exc:  # noqa: BLE001
            self._record_error(f"master.idx {year}Q{quarter}", url, exc)
            return

        yield from self._parse_text(text, scope_set, cik_filter)

    def _parse_text(
        self,
        text: str,
        scope_set: set[FormType] | None,
        cik_filter: set[str] | None,
    ) -> Iterator[FilingRecord]:
        for line in text.splitlines():
            # Data rows have exactly 5 pipe-delimited fields and start with digits.
            parts = line.split("|")
            if len(parts) != 5 or not parts[0].strip().isdigit():
                continue
            raw_cik, company, raw_form, filed, filename = (p.strip() for p in parts)

            ft = from_edgar_form(raw_form)
            if ft is None:
                continue
            if scope_set is not None and ft not in scope_set:
                continue

            cik = normalize_cik(raw_cik)
            if cik_filter is not None and cik not in cik_filter:
                continue

            # filename: edgar/data/<cik>/<accession-with-dashes>.txt
            accession = filename.rsplit("/", 1)[-1].removesuffix(".txt")

            yield FilingRecord(
                cik=cik,
                form_type=ft,
                sec_form=raw_form,
                accession=accession,
                title=f"{company} {raw_form}".strip(),
                company=company,
                filing_date=_parse_date(filed),
                submission_url=ARCHIVES_BASE + filename,
                provenance="edgar_index",
            )
