"""Shared helpers for the vault-contract tests: FilingRecord fixtures of every vault family
and the contract package's checks with this corpus's generated vocabulary."""
from __future__ import annotations

from datetime import date

from vault_contract import check_row, load_vocabulary

from company_corpus.config import Config
from company_corpus.models import FilingRecord
from company_corpus.taxonomy import FormType
from company_corpus.vocabulary import build_vocabulary

CORPUS = "company"


def vocabulary(config: Config | None = None):
    return load_vocabulary(build_vocabulary(config))


def check(row: dict, *, transition: bool = True, config: Config | None = None):
    return check_row(row, vocabulary(config), expected_corpus=CORPUS, transition=transition)


def reasons(items) -> list[str]:
    return [item.reason for item in items]


def record(form_type: FormType, *, cik: str = "320193", n: int = 1, **overrides) -> FilingRecord:
    """A downloaded and rendered filing of ``form_type`` (every artefact pointer set)."""
    sec_form = form_type.edgar_forms[0]
    accession = f"0000320193-24-{n:06d}"
    base = dict(
        cik=cik, form_type=form_type, sec_form=sec_form, accession=accession,
        title=f"Apple Inc. {sec_form} (2024)", company="Apple Inc.",
        company_current="Apple Inc.", ticker="AAPL",
        filing_date=date(2024, 11, 1), period_of_report=date(2024, 9, 28),
        primary_doc_url=f"https://www.sec.gov/Archives/edgar/data/320193/"
                        f"{accession.replace('-', '')}/doc{n}.htm",
        submission_url=f"https://www.sec.gov/Archives/edgar/data/320193/"
                       f"{accession.replace('-', '')}/{accession}.txt",
        provenance="edgar_submissions",
    )
    base.update(overrides)
    rec = FilingRecord(**base)
    d = f"raw/{rec.cik}/{form_type.code}/2024/{rec.doc_id}"
    for name, value in (("submission_path", f"{d}.submission.txt"),
                        ("submission_sha256", "b" * 64), ("primary_sha256", "a" * 64),
                        ("primary_path", f"{d}.primary.htm"), ("text_path", f"{d}.txt"),
                        ("pdf_path", f"{d}.pdf")):
        if name not in overrides:
            setattr(rec, name, value)
    return rec


#: One form of every vault family A-E (spec §2 Q2).
VAULT_FORMS: tuple[FormType, ...] = tuple(ft for ft in FormType if ft.family in "ABCDE")
