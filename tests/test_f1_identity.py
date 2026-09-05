"""A 10-K reports the current year AND its comparatives with one accession, so
an F1 doc_id keyed on the accession alone loses a fiscal year (DI-C2)."""
from __future__ import annotations

from datetime import date

import pytest

from company_corpus.config import normalize_cik
from company_corpus.models import FilingRecord, IdentityCollisionError
from company_corpus.pipeline import fetch_financials
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType


def _annual(start: str, end: str, val: int) -> dict:
    return {"start": start, "end": end, "val": val, "accn": "acc-fy24",
            "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"}


def _instant(end: str, val: int) -> dict:
    return {"end": end, "val": val, "accn": "acc-fy24",
            "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"}


# One 10-K, filed once, introducing three period-ends: the real shape of a
# companyfacts feed's first filing. All nine points share `accn` and `filed`.
_ONE_FILING_THREE_YEARS = {
    "cik": 320193,
    "entityName": "Acme Inc.",
    "facts": {"us-gaap": {
        "Revenues": {"label": "Revenues", "units": {"USD": [
            _annual("2023-10-01", "2024-09-30", 300),
            _annual("2022-10-01", "2023-09-30", 200),
            _annual("2021-10-01", "2022-09-30", 100),
        ]}},
        "Assets": {"label": "Assets", "units": {"USD": [
            _instant("2024-09-30", 30),
            _instant("2023-09-30", 20),
            _instant("2022-09-30", 10),
        ]}},
    }},
}


def _f1(period_end, accession="acc-fy24"):
    return FilingRecord(cik="320193", form_type=FormType.F1, sec_form="10-K/XBRL",
                        accession=accession, period_of_report=period_end)


def test_two_periods_of_one_accession_have_distinct_doc_ids():
    assert _f1(date(2024, 9, 30)).doc_id != _f1(date(2023, 9, 30)).doc_id


def test_f1_without_a_period_still_has_a_stable_doc_id():
    # Adversarial: a synthetic period summary whose period-end never parsed.
    # It must not raise, and must stay distinct from any dated sibling.
    undated = _f1(None)
    assert undated.doc_id == _f1(None).doc_id
    assert undated.doc_id != _f1(date(2024, 9, 30)).doc_id


def test_family_a_doc_id_is_unchanged():
    # 6.4 GB of family-A artefacts are named by this hash: it must not drift.
    rec = FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K",
                       accession="0000320193-24-000123",
                       period_of_report=date(2024, 9, 30))
    assert rec.doc_id == FilingRecord(
        cik="320193", form_type=FormType.A1, sec_form="10-K",
        accession="0000320193-24-000123").doc_id


class _ThreeYearSource:
    """EdgarXBRL stand-in serving the one-filing/three-periods companyfacts."""

    def __init__(self, **_):
        self.errors: list[dict] = []

    def period_summaries(self, cik, **_):
        from company_corpus.financials import build_period_summaries
        return _ONE_FILING_THREE_YEARS, build_period_summaries(
            _ONE_FILING_THREE_YEARS, company="Acme Inc.", company_current="Acme Inc.")


def test_three_comparatives_reach_three_manifest_rows(config, monkeypatch):
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _ThreeYearSource)
    report = fetch_financials(["320193"], dry_run=False, config=config,
                              fetcher=object(), storage=Storage(config))
    assert report.periods == 3
    assert len(Storage(config).load_manifest("320193")) == 3
    html = sorted(
        (config.raw_dir / normalize_cik("320193") / "F1").rglob("*.primary.html"))
    assert len(html) == 3


def test_a_genuine_collision_raises(config, monkeypatch):
    class _Source:
        def __init__(self, **_):
            self.errors: list[dict] = []

        def period_summaries(self, cik, **_):
            from company_corpus.financials import PeriodSummary
            duplicate = PeriodSummary(
                period_end=date(2024, 9, 30), frequency="annual",
                publication_date=date(2024, 11, 1), sec_form="10-K",
                accession="acc-fy24", company="Acme", company_current="Acme",
                values={"assets": {"value": 1, "unit": "USD", "label": "Assets"}})
            return {"facts": {}}, [duplicate, duplicate]

    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _Source)
    with pytest.raises(IdentityCollisionError):
        fetch_financials(["320193"], dry_run=False, config=config,
                         fetcher=object(), storage=Storage(config))


def test_a_genuine_collision_writes_no_summary_artefact(config, monkeypatch):
    """The check runs before any write: a colliding run leaves no half-corpus."""
    class _Source:
        def __init__(self, **_):
            self.errors: list[dict] = []

        def period_summaries(self, cik, **_):
            from company_corpus.financials import PeriodSummary
            duplicate = PeriodSummary(
                period_end=date(2024, 9, 30), frequency="annual",
                publication_date=date(2024, 11, 1), sec_form="10-K",
                accession="acc-fy24", company="Acme", company_current="Acme",
                values={"assets": {"value": 1, "unit": "USD", "label": "Assets"}})
            return {"facts": {}}, [duplicate, duplicate]

    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _Source)
    with pytest.raises(IdentityCollisionError):
        fetch_financials(["320193"], dry_run=False, config=config,
                         fetcher=object(), storage=Storage(config))
    assert not list(config.raw_dir.rglob("*.primary.html"))
    assert not config.financials_dir.exists()
