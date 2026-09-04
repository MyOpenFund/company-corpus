"""Per-issuer discovery via the EDGAR submissions API.

``https://data.sec.gov/submissions/CIK##########.json`` returns a company's
entire filing history. Recent filings live under ``filings.recent`` (parallel
arrays); older filings are split into additional JSON files referenced by
``filings.files``. This source is the workhorse for the curated issuer tier.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date

from ..config import normalize_cik
from ..models import FilingRecord
from ..naming import NamePeriod, name_as_of, parse_former_names
from ..taxonomy import FormType, from_edgar_form
from .base import Source, cik_to_path_int

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
EXTRA_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:  # pragma: no cover - defensive
        return None


class EdgarSubmissions(Source):
    """Yield filings for a CIK from the submissions API, filtered by scope."""

    name = "edgar_submissions"

    def discover(
        self,
        cik: str,
        scope: Sequence[FormType] = (),
        since: date | None = None,
    ) -> Iterator[FilingRecord]:
        cik = normalize_cik(cik)
        scope_set = set(scope) if scope else None
        url = SUBMISSIONS_URL.format(cik=cik)
        try:
            data = self.fetcher.get_json(url)
        except Exception as exc:  # noqa: BLE001 - record and stop for this CIK
            self._record_error("submissions", url, exc)
            return

        company = data.get("name", "")
        tickers = data.get("tickers") or []
        ticker = tickers[0] if tickers else ""
        former = parse_former_names(data.get("formerNames"))

        filings = data.get("filings", {})
        recent = filings.get("recent", {})
        yield from self._parse_block(cik, company, ticker, former, recent, scope_set, since)

        for extra in filings.get("files", []):
            extra_url = EXTRA_URL.format(name=extra.get("name", ""))
            try:
                block = self.fetcher.get_json(extra_url)
            except Exception as exc:  # noqa: BLE001
                self._record_error("submissions-extra", extra_url, exc)
                continue
            yield from self._parse_block(cik, company, ticker, former, block, scope_set, since)

    def _parse_block(
        self,
        cik: str,
        company: str,
        ticker: str,
        former: list[NamePeriod],
        block: dict,
        scope_set: set[FormType] | None,
        since: date | None,
    ) -> Iterator[FilingRecord]:
        forms = block.get("form", [])
        accessions = block.get("accessionNumber", [])
        filing_dates = block.get("filingDate", [])
        report_dates = block.get("reportDate", [])
        primary_docs = block.get("primaryDocument", [])
        primary_descs = block.get("primaryDocDescription", [])

        cik_int = cik_to_path_int(cik)
        for i, raw_form in enumerate(forms):
            ft = from_edgar_form(raw_form)
            if ft is None:
                continue
            if scope_set is not None and ft not in scope_set:
                continue

            filing_date = _parse_date(filing_dates[i] if i < len(filing_dates) else None)
            if since and filing_date and filing_date < since:
                continue

            accession = accessions[i] if i < len(accessions) else ""
            if not accession:
                continue
            acc_nodash = accession.replace("-", "")
            base = ARCHIVES.format(cik_int=cik_int, acc_nodash=acc_nodash)
            primary = primary_docs[i] if i < len(primary_docs) else ""
            desc = primary_descs[i] if i < len(primary_descs) else ""

            # Attribute the filing to the name in effect when it was filed.
            pit_name = name_as_of(filing_date, company, former)

            yield FilingRecord(
                cik=cik,
                form_type=ft,
                sec_form=raw_form,
                accession=accession,
                title=f"{pit_name} {raw_form}".strip() + (f" — {desc}" if desc else ""),
                company=pit_name,
                company_current=company,
                ticker=ticker,
                filing_date=filing_date,
                period_of_report=_parse_date(report_dates[i] if i < len(report_dates) else None),
                primary_doc_url=f"{base}/{primary}" if primary else "",
                submission_url=f"{base}/{accession}.txt",
                provenance="edgar_submissions",
            )
