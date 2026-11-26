"""Filing listing from the EDGAR submissions API, fetched on demand (no on-disk corpus)."""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..config import Config, normalize_cik
from ..opendata.fundamentals import source_doc_id
from ..opendata.submissions import (
    SubmissionsArchive,
    archive_from_bytes,
    filing_blocks,
    main_name,
    page_names,
)
from ..sources.base import cik_to_path_int
from ..sources.edgar_submissions import ARCHIVES, EXTRA_URL, SUBMISSIONS_URL
from .sec import SecGet

_MAX_NAME = Config.__dataclass_fields__["max_path_component_length"].default


@dataclass(frozen=True)
class FilingRef:
    cik: str
    accession: str
    form: str
    report_date: date | None
    filed: date | None
    accepted: datetime | None
    primary_document: str | None
    url: str  # the EDGAR folder of the filing
    doc_id: str | None


def fetch_submissions(cik: str, sec: SecGet, *, max_age_s: float | None,
                      now: datetime) -> SubmissionsArchive:
    """The main submissions file plus every ``filings.files`` page, at the URLs
    ``fetch_archive`` uses."""
    cik = normalize_cik(cik)
    main = sec.get_bytes(SUBMISSIONS_URL.format(cik=cik), max_age_s=max_age_s)
    names = page_names(json.loads(main), max_length=_MAX_NAME)
    pages = [(n, sec.get_bytes(EXTRA_URL.format(name=n), max_age_s=max_age_s)) for n in names]
    return archive_from_bytes(
        cik, main, pages, fetched_at=now,
        url_of=lambda name: (SUBMISSIONS_URL.format(cik=cik) if name == main_name(cik)
                             else EXTRA_URL.format(name=name)))


def _day(raw) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _when(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        t = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return t.astimezone(timezone.utc) if t.tzinfo is not None else None


def _base(form: str) -> str:
    return form[:-2] if form.endswith("/A") else form


def list_filings(archive: SubmissionsArchive, *, forms: Collection[str] | None,
                 period_from: date | None, period_to: date | None) -> list[FilingRef]:
    """Filings of the archive, newest accepted first. A ``/A`` form matches its base form;
    the period window applies to ``report_date`` (a filing without one is excluded when a
    bound is set). An accession listed twice keeps its first listing."""
    wanted = {f.upper() for f in forms} if forms is not None else None
    cik = archive.cik
    seen: set[str] = set()
    refs: list[FilingRef] = []
    for block in filing_blocks(archive):
        def col(name, i):
            values = block.get(name) or []
            return (values[i] or None) if i < len(values) else None
        for i, accn in enumerate(block.get("accessionNumber") or []):
            form = col("form", i)
            if not accn or accn in seen or not isinstance(form, str):
                continue
            seen.add(accn)
            upper = form.upper()
            if wanted is not None and upper not in wanted and _base(upper) not in wanted:
                continue
            report = _day(col("reportDate", i))
            if (period_from or period_to) and report is None:
                continue
            if (period_from and report < period_from) or (period_to and report > period_to):
                continue
            doc = col("primaryDocument", i)
            refs.append(FilingRef(
                cik=cik, accession=accn, form=form, report_date=report,
                filed=_day(col("filingDate", i)), accepted=_when(col("acceptanceDateTime", i)),
                primary_document=doc if isinstance(doc, str) else None,
                url=ARCHIVES.format(cik_int=cik_to_path_int(cik), acc_nodash=accn.replace("-", "")),
                doc_id=source_doc_id(cik, form, accn, frozenset(), Counter())))
    refs.sort(key=lambda r: (r.accepted is not None, r.accepted or datetime.min.replace(
        tzinfo=timezone.utc), r.filed or date.min, r.accession), reverse=True)
    return refs
