"""Canonical filing-record model for the bottom-up corpus.

Parallels ``cb_corpus.models.DocRecord``. A :class:`FilingRecord` is the unit of
the per-issuer manifest. Its ``doc_id`` is a stable, date-independent hash keyed
on ``cik | form_type | accession`` so that re-runs are idempotent and metadata
corrections (e.g. a refined ``filing_date``) never change a document's identity
or force a re-download. The families listed in
:data:`DOC_ID_PERIOD_KEYED_FAMILIES` add the reported period -- the pair
``period_of_report | frequency`` -- to that basis, because their records are
synthetic pseudo-filings that share one accession across several periods.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from .config import DEFAULT_VAULT_ARTIFACT_PREFERENCE, normalize_cik
from .taxonomy import FormType, by_code

#: The vault ``documents`` contract (vault repo, ``contract/``) this module writes: every
#: manifest row is one contract row of corpus ``company``. The runtime never imports the
#: contract package; tests check the rows against it.
CORPUS = "company"
#: The authority of every FilingRecord (``source_codes.SOURCE_CODES``): SEC EDGAR.
SEC_SOURCE_CODE = "sec"
#: An EDGAR filing date is a day, and it is the day the document was published.
SEC_DATE_PRECISION = "day"
SEC_DATE_KIND = "publication"
#: The identifier scheme of ``entity_value``: the issuer's 10-digit CIK.
SEC_ENTITY_SCHEME = "cik"

#: A vault artefact kind -> (the record attribute pointing at it, the file's media type).
VAULT_ARTIFACTS: dict[str, tuple[str, str]] = {
    "pdf": ("pdf_path", "application/pdf"),
    "text": ("text_path", "text/plain"),
}

EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
_ACCESSION = re.compile(r"\A[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")


def index_url(cik: str, accession: str) -> str | None:
    """The EDGAR filing index page of a filing, or None for a non-EDGAR accession.

    ``.../edgar/data/<cik without zeros>/<accession without dashes>/<accession>-index.htm``.
    """
    if not isinstance(accession, str) or not _ACCESSION.match(accession):
        return None
    return (f"{EDGAR_ARCHIVES}/{int(normalize_cik(cik))}/{accession.replace('-', '')}/"
            f"{accession}-index.htm")


def artifact_for_vault(record: "FilingRecord",
                       preference: Sequence[str]) -> tuple[str | None, str | None]:
    """``(local_path, mime_type)``: the first artefact of ``preference`` the record points at.

    Decided by the pointer being set, never by a disk read, so the row is the same on every
    host. ``(None, None)`` when the record has none of them (not yet downloaded, or a filing
    with no primary document): the row stays in the manifest as inventory.
    """
    for kind in preference:
        attr, mime = VAULT_ARTIFACTS[kind]
        path = getattr(record, attr)
        if path:
            return path, mime
    return None, None

#: Taxonomy families whose ``doc_id`` also keys on the reported period, i.e. on
#: the pair ``period_of_report | frequency``.
#: Family F (XBRL period summaries) is a SYNTHETIC pseudo-filing: its
#: ``accession`` is the accession of the filing that first reported the period,
#: and one 10-K reports the current year AND its comparatives, so the accession
#: alone collapses two or three fiscal years onto one id -- measured at 19 % of
#: all periods across 40 real issuers (DI-C2). The period is a PAIR, not a date:
#: ``financials.summaries_from_flat`` buckets on ``(end, frequency)``, and one
#: 10-K tags the Q4 three-month duration alongside the twelve-month one, both
#: ending the same day -- so ``frequency`` is part of the basis or those two
#: summaries would be one document. ``accession`` itself keeps the real EDGAR
#: value: it is provenance, and the vault contract reads it.
#: Adding a family here CHANGES identity for that family; never do it for a
#: family with artefacts already on disk.
DOC_ID_PERIOD_KEYED_FAMILIES: frozenset[str] = frozenset({"F"})


class IdentityCollisionError(RuntimeError):
    """Two records of one issuer computed the same ``doc_id``.

    ``doc_ids`` carries the colliding ids so a caller can report them as
    structured data rather than re-parsing the message.
    """

    def __init__(self, message: str, doc_ids: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.doc_ids: list[str] = list(doc_ids)


class ManifestNeedsMigration(RuntimeError):
    """A manifest row in the pre-contract form (written before chantier 4).

    Only ``migrate-contract`` reads that form (``migrate.contract_names``). Every other reader
    fails on it, never skips it: a skipped row would be dropped by the next write of its
    manifest. Not a ``ValueError``/``KeyError``/``TypeError``, so the corrupt-line tolerance of
    the manifest reader cannot swallow it.
    """


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
    # Reporting frequency of the period (annual | quarterly | semi-annual), set
    # only by producers of period-keyed synthetic records (family F). It is part
    # of the doc_id basis for those families -- never derive it from a label.
    frequency: str = ""

    primary_doc_url: str = ""             # the report document itself
    submission_url: str = ""              # the complete-submission .txt

    provenance: str = "edgar_index"       # edgar_index | edgar_fts | edgar_submissions | wayback
    submission_sha256: str | None = None  # hash of the stored submission (integrity/dedup)
    #: Hash of the primary document as the authority served it (the bytes decomposed out of
    #: the submission; family F: the summary HTML). The contract row's ``sha256``. It stays
    #: when an ownership summary later supersedes the file at ``primary_path``.
    primary_sha256: str | None = None

    submission_path: str | None = None    # stored full submission (rel. to the data dir)
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

        For :data:`DOC_ID_PERIOD_KEYED_FAMILIES` the period the record covers --
        ``period_of_report`` AND ``frequency`` -- is part of the basis; see that
        constant.
        """
        basis = f"{self.cik}|{self.form_type.code}|{self.accession}"
        if self.form_type.family in DOC_ID_PERIOD_KEYED_FAMILIES:
            period = self.period_of_report.isoformat() if self.period_of_report else ""
            basis = f"{basis}|{period}|{self.frequency}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    @property
    def year(self) -> int | None:
        return self.filing_date.year if self.filing_date else None

    def to_row(self, *, artifact_preference: Sequence[str] = DEFAULT_VAULT_ARTIFACT_PREFERENCE
               ) -> dict:
        """Serialize to one manifest line: a row of the vault ``documents`` contract.

        The 18 contract fields first (in the contract's order), then this corpus's extras.
        ``artifact_preference`` is ``Config.vault_artifact_preference`` (see
        :func:`artifact_for_vault`).
        """
        dated = self.filing_date is not None
        local_path, mime_type = artifact_for_vault(self, artifact_preference)
        return {
            "doc_id": self.doc_id,
            "corpus": CORPUS,
            "source_code": SEC_SOURCE_CODE,
            "doc_type": self.form_type.code,
            "title": self.title,
            "date": self.filing_date.isoformat() if dated else None,
            "year": self.year,
            "date_precision": SEC_DATE_PRECISION if dated else None,
            "date_kind": SEC_DATE_KIND if dated else None,
            "language": self.language,
            "provenance": self.provenance,
            "document_url": self.primary_doc_url or None,
            "source_url": index_url(self.cik, self.accession),
            "mime_type": mime_type,
            "sha256": self.primary_sha256,
            "local_path": local_path,
            "entity_scheme": SEC_ENTITY_SCHEME,
            "entity_value": self.cik,
            # extras
            "cik": self.cik,
            "family": self.form_type.family,
            "sec_form": self.sec_form,
            "accession": self.accession,
            "period_of_report": (
                self.period_of_report.isoformat() if self.period_of_report else None),
            "company": self.company,
            "company_current": self.company_current,
            "ticker": self.ticker,
            "entity_id": self.entity_id,
            "primary_path": self.primary_path,
            "text_path": self.text_path,
            "pdf_path": self.pdf_path,
            "submission_path": self.submission_path,
            "submission_sha256": self.submission_sha256,
            "submission_url": self.submission_url,
            "alt_urls": list(self.alt_urls),
            "frequency": self.frequency,
        }

    @classmethod
    def from_row(cls, row: dict) -> "FilingRecord":
        """Reconstruct a record from a manifest line in the contract form (:meth:`to_row`).

        A pre-contract row (no ``doc_type``, the type under ``form_type``) raises
        :class:`ManifestNeedsMigration`: run ``migrate-contract --write`` first.
        """
        def _parse_date(value: str | None) -> date | None:
            return date.fromisoformat(value) if value else None

        if "doc_type" not in row and "form_type" in row:
            raise ManifestNeedsMigration(
                f"a manifest row in the pre-contract form (doc_id {row.get('doc_id')!r}, "
                "form_type instead of doc_type); run `company-corpus migrate-contract --write` "
                "first (see docs/INGESTION_RAG.md, Migrating)")
        return cls(
            cik=row["cik"],
            form_type=by_code(row["doc_type"]),
            sec_form=row["sec_form"],
            accession=row["accession"],
            title=row.get("title", ""),
            company=row.get("company", ""),
            company_current=row.get("company_current", ""),
            ticker=row.get("ticker", ""),
            entity_id=row.get("entity_id", ""),
            filing_date=_parse_date(row.get("date")),
            period_of_report=_parse_date(row.get("period_of_report")),
            frequency=row.get("frequency") or "",
            primary_doc_url=row.get("document_url") or "",
            submission_url=row.get("submission_url", ""),
            provenance=row.get("provenance", "edgar_index"),
            submission_sha256=row.get("submission_sha256"),
            primary_sha256=row.get("sha256"),
            submission_path=row.get("submission_path"),
            primary_path=row.get("primary_path"),
            text_path=row.get("text_path"),
            pdf_path=row.get("pdf_path"),
            language=row.get("language", "en"),
            alt_urls=list(row.get("alt_urls", [])),
        )
