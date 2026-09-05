"""Canonical filing-record model for the bottom-up corpus.

Parallels ``cb_corpus.models.DocRecord``. A :class:`FilingRecord` is the unit of
the per-issuer manifest. Its ``doc_id`` is a stable, date-independent hash keyed
on ``cik | form_type | accession`` so that re-runs are idempotent and metadata
corrections (e.g. a refined ``filing_date``) never change a document's identity
or force a re-download. The families listed in
:data:`DOC_ID_PERIOD_KEYED_FAMILIES` add the reported period to that basis,
because their records are synthetic pseudo-filings that share one accession
across several periods.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date

from .config import normalize_cik
from .taxonomy import FormType, by_code

#: Taxonomy families whose ``doc_id`` also keys on ``period_of_report``.
#: Family F (XBRL period summaries) is a SYNTHETIC pseudo-filing: its
#: ``accession`` is the accession of the filing that first reported the period,
#: and one 10-K reports the current year AND its comparatives, so the accession
#: alone collapses two or three fiscal years onto one id -- measured at 19 % of
#: all periods across 40 real issuers (DI-C2). ``accession`` itself keeps the
#: real EDGAR value: it is provenance, and the vault contract reads it.
#: Adding a family here CHANGES identity for that family; never do it for a
#: family with artefacts already on disk.
DOC_ID_PERIOD_KEYED_FAMILIES: frozenset[str] = frozenset({"F"})


class IdentityCollisionError(RuntimeError):
    """Two records of one issuer computed the same ``doc_id``."""


@dataclass
class FilingRecord:
    """One company filing, with provenance and on-disk pointers."""

    cik: str                              # zero-padded 10-digit CIK
    form_type: FormType                   # taxonomy family (serialized as code)
    sec_form: str                         # raw EDGAR form, e.g. "10-K"
    accession: str                        # EDGAR accession number (natural key)
    title: str = ""
    company: str = ""                     # name in effect on filing_date (point-in-time)
    company_current: str = ""             # current registrant name (for search/joins)
    ticker: str = ""
    entity_id: str = ""                   # canonical entity id (cross-CIK alias), if any

    filing_date: date | None = None       # date EDGAR accepted the filing (day precision)
    period_of_report: date | None = None  # fiscal period the filing covers

    primary_doc_url: str = ""             # the report document itself
    submission_url: str = ""              # the complete-submission .txt

    provenance: str = "edgar_index"       # edgar_index | edgar_fts | edgar_submissions | wayback
    sha256: str | None = None             # hash of stored submission (integrity/dedup)

    local_path: str | None = None         # stored full submission (rel. to data/)
    primary_path: str | None = None       # decomposed primary document
    text_path: str | None = None          # cleaned extracted text
    pdf_path: str | None = None           # populated by the separate render-pdf batch

    language: str = "en"
    alt_urls: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cik = normalize_cik(self.cik)
        if isinstance(self.form_type, str):
            self.form_type = by_code(self.form_type)

    @property
    def doc_id(self) -> str:
        """Stable 16-char hex id keyed on cik|form|accession (date-independent).

        For :data:`DOC_ID_PERIOD_KEYED_FAMILIES` the period the record covers is
        part of the basis -- see that constant.
        """
        basis = f"{self.cik}|{self.form_type.code}|{self.accession}"
        if self.form_type.family in DOC_ID_PERIOD_KEYED_FAMILIES:
            period = self.period_of_report.isoformat() if self.period_of_report else ""
            basis = f"{basis}|{period}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    @property
    def year(self) -> int | None:
        return self.filing_date.year if self.filing_date else None

    def to_row(self) -> dict:
        """Serialize to a JSON-ready dict (one manifest line)."""
        row = asdict(self)
        row["form_type"] = self.form_type.code
        row["family"] = self.form_type.family
        row["filing_date"] = self.filing_date.isoformat() if self.filing_date else None
        row["period_of_report"] = (
            self.period_of_report.isoformat() if self.period_of_report else None
        )
        row["doc_id"] = self.doc_id
        row["year"] = self.year
        return row

    @classmethod
    def from_row(cls, row: dict) -> "FilingRecord":
        """Reconstruct a record from a manifest line (inverse of :meth:`to_row`)."""
        def _parse_date(value: str | None) -> date | None:
            return date.fromisoformat(value) if value else None

        return cls(
            cik=row["cik"],
            form_type=by_code(row["form_type"]),
            sec_form=row["sec_form"],
            accession=row["accession"],
            title=row.get("title", ""),
            company=row.get("company", ""),
            company_current=row.get("company_current", ""),
            ticker=row.get("ticker", ""),
            entity_id=row.get("entity_id", ""),
            filing_date=_parse_date(row.get("filing_date")),
            period_of_report=_parse_date(row.get("period_of_report")),
            primary_doc_url=row.get("primary_doc_url", ""),
            submission_url=row.get("submission_url", ""),
            provenance=row.get("provenance", "edgar_index"),
            sha256=row.get("sha256"),
            local_path=row.get("local_path"),
            primary_path=row.get("primary_path"),
            text_path=row.get("text_path"),
            pdf_path=row.get("pdf_path"),
            language=row.get("language", "en"),
            alt_urls=list(row.get("alt_urls", [])),
        )
