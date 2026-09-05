"""A 10-K reports the current year AND its comparatives with one accession, so
an F1 doc_id keyed on the accession alone loses a fiscal year (DI-C2).

A *period* here is the pair ``(period_end, frequency)`` — the same key
``financials.summaries_from_flat`` buckets on — because one 10-K also tags the
Q4 three-month duration alongside the twelve-month one, both ending on the same
day. Identity keyed on the end date alone would call those two summaries the
same document.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from company_corpus import cli
from company_corpus.config import normalize_cik
from company_corpus.models import FilingRecord, IdentityCollisionError
from company_corpus.pipeline import _assert_unique_doc_ids, fetch_financials
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType


def _duration(start: str, end: str, val: int) -> dict:
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
            _duration("2023-10-01", "2024-09-30", 300),
            _duration("2022-10-01", "2023-09-30", 200),
            _duration("2021-10-01", "2022-09-30", 100),
        ]}},
        "Assets": {"label": "Assets", "units": {"USD": [
            _instant("2024-09-30", 30),
            _instant("2023-09-30", 20),
            _instant("2022-09-30", 10),
        ]}},
    }},
}

# The real Q4 shape: one 10-K tagging the twelve-month AND the three-month
# duration that end on the SAME day, under one accession. Grouped by
# (period_end, frequency) that is two summaries, so it is two documents.
_ONE_FILING_ANNUAL_AND_Q4 = {
    "cik": 320193,
    "entityName": "Acme Inc.",
    "facts": {"us-gaap": {
        "Revenues": {"label": "Revenues", "units": {"USD": [
            _duration("2023-10-01", "2024-09-30", 300),   # FY2024, 365 d
            _duration("2024-07-01", "2024-09-30", 80),    # Q4 2024, 91 d
        ]}},
        "Assets": {"label": "Assets", "units": {"USD": [_instant("2024-09-30", 30)]}},
    }},
}


def _f1(period_end, accession="acc-fy24", frequency="annual"):
    return FilingRecord(cik="320193", form_type=FormType.F1, sec_form="10-K/XBRL",
                        accession=accession, period_of_report=period_end,
                        frequency=frequency)


def test_two_periods_of_one_accession_have_distinct_doc_ids():
    assert _f1(date(2024, 9, 30)).doc_id != _f1(date(2023, 9, 30)).doc_id


def test_annual_and_quarterly_ending_the_same_day_have_distinct_doc_ids():
    # A period is (end, frequency): a 10-K's FY and its Q4 share the end date.
    assert (_f1(date(2024, 9, 30), frequency="annual").doc_id
            != _f1(date(2024, 9, 30), frequency="quarterly").doc_id)


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


def test_family_a_ignores_frequency_in_its_doc_id():
    # frequency is part of the basis only for the period-keyed families.
    rec = FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K",
                       accession="0000320193-24-000123", frequency="annual")
    assert rec.doc_id == FilingRecord(
        cik="320193", form_type=FormType.A1, sec_form="10-K",
        accession="0000320193-24-000123").doc_id


def test_frequency_round_trips_through_a_manifest_row():
    # A reloaded record must hash identically, or a re-run would orphan the
    # artefact it just wrote.
    rec = _f1(date(2024, 9, 30), frequency="quarterly")
    row = rec.to_row()
    assert row["frequency"] == "quarterly"
    back = FilingRecord.from_row(json.loads(json.dumps(row)))
    assert back.frequency == "quarterly"
    assert back.doc_id == rec.doc_id


# --------------------------------------------------------------------------
# EdgarXBRL stand-ins
# --------------------------------------------------------------------------
def _summary(period_end: date, frequency: str, accession: str = "acc-fy24"):
    from company_corpus.financials import PeriodSummary
    return PeriodSummary(
        period_end=period_end, frequency=frequency,
        publication_date=date(2024, 11, 1), sec_form="10-K",
        accession=accession, company="Acme", company_current="Acme",
        values={"assets": {"value": 1, "unit": "USD", "label": "Assets"}})


class _FactsSource:
    """EdgarXBRL stand-in grouping a canned companyfacts payload."""

    facts: dict = _ONE_FILING_THREE_YEARS

    def __init__(self, **_):
        self.errors: list[dict] = []

    def period_summaries(self, cik, **_):
        from company_corpus.financials import build_period_summaries
        return self.facts, build_period_summaries(
            self.facts, company="Acme Inc.", company_current="Acme Inc.")


class _ThreeYearSource(_FactsSource):
    facts = _ONE_FILING_THREE_YEARS


class _AnnualAndQ4Source(_FactsSource):
    facts = _ONE_FILING_ANNUAL_AND_Q4


class _DuplicateSource:
    """Serves one period summary twice: a genuine, non-recoverable collision."""

    def __init__(self, **_):
        self.errors: list[dict] = []

    def period_summaries(self, cik, **_):
        duplicate = _summary(date(2024, 9, 30), "annual")
        return {"facts": {}}, [duplicate, duplicate]


class _OneCollidingIssuerSource:
    """CIK 320193 collides; every other issuer is clean and productive."""

    colliding = normalize_cik("320193")

    def __init__(self, **_):
        self.errors: list[dict] = []

    def period_summaries(self, cik, **_):
        if normalize_cik(cik) == self.colliding:
            duplicate = _summary(date(2024, 9, 30), "annual")
            return {"facts": {}}, [duplicate, duplicate]
        return {"facts": {}}, [_summary(date(2024, 9, 30), "annual", "acc-other")]


# --------------------------------------------------------------------------
# Pipeline behaviour
# --------------------------------------------------------------------------
def test_three_comparatives_reach_three_manifest_rows(config, monkeypatch):
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _ThreeYearSource)
    report = fetch_financials(["320193"], dry_run=False, config=config,
                              fetcher=object(), storage=Storage(config))
    assert report.periods == 3
    assert len(Storage(config).load_manifest("320193")) == 3
    html = sorted(
        (config.raw_dir / normalize_cik("320193") / "F1").rglob("*.primary.html"))
    assert len(html) == 3


def test_annual_and_q4_of_one_accession_reach_two_manifest_rows(config, monkeypatch):
    """The reviewer's 47/60-issuer case: same accession, same period_end, two
    frequencies. It is two documents, not a collision."""
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _AnnualAndQ4Source)
    report = fetch_financials(["320193"], dry_run=False, config=config,
                              fetcher=object(), storage=Storage(config))
    assert not report.errors
    assert report.periods == 2
    manifest = Storage(config).load_manifest("320193")
    assert len(manifest) == 2
    assert {r.frequency for r in manifest.values()} == {"annual", "quarterly"}
    html = sorted(
        (config.raw_dir / normalize_cik("320193") / "F1").rglob("*.primary.html"))
    assert len(html) == 2


def test_a_genuine_collision_raises():
    # The identity assertion itself still fails loudly for a true duplicate.
    rec = _f1(date(2024, 9, 30))
    with pytest.raises(IdentityCollisionError) as exc:
        _assert_unique_doc_ids("320193", [rec, rec])
    assert exc.value.doc_ids == [rec.doc_id]


def test_a_genuine_collision_writes_no_summary_artefact(config, monkeypatch):
    """The check runs before any write: a colliding issuer leaves no half-corpus,
    and is reported as a per-issuer error rather than aborting the process."""
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _DuplicateSource)
    report = fetch_financials(["320193"], dry_run=False, config=config,
                              fetcher=object(), storage=Storage(config))
    assert report.periods == 0
    assert not list(config.raw_dir.rglob("*.primary.html"))
    assert not config.financials_dir.exists()
    assert len(report.errors) == 1
    err = report.errors[0]
    assert err["context"] == normalize_cik("320193")
    assert err["doc_ids"] == [_f1(date(2024, 9, 30)).doc_id]
    assert "collid" in err["error"].lower()
    # The error is on the audit trail too, not only in the in-memory report.
    trail = config.discovery_errors_path.read_text(encoding="utf-8")
    assert normalize_cik("320193") in trail


def test_a_colliding_issuer_does_not_abort_the_other_issuers(config, monkeypatch):
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _OneCollidingIssuerSource)
    report = fetch_financials(["320193", "789019"], dry_run=False, config=config,
                              fetcher=object(), storage=Storage(config))
    storage = Storage(config)
    assert not storage.load_manifest("320193")           # write-nothing for that one
    assert len(storage.load_manifest("789019")) == 1     # the others still run
    assert report.periods == 1
    assert [e["context"] for e in report.errors] == [normalize_cik("320193")]


# --------------------------------------------------------------------------
# Exit-code doctrine (runreport.finish decides, not the pipeline)
# --------------------------------------------------------------------------
def _run_xbrl(monkeypatch, tmp_path, ciks: str) -> tuple[int, dict]:
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["--data-dir", str(tmp_path), "xbrl", "--ciks", ciks, "--write"])
    lines = (tmp_path / "runs.jsonl").read_text().strip().split("\n")
    return rc, json.loads(lines[-1])


def test_only_colliding_issuers_degrades_the_run(monkeypatch, tmp_path):
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _DuplicateSource)
    rc, rep = _run_xbrl(monkeypatch, tmp_path, "0000320193")
    assert rc == 3 and rep["outcome"] == "degraded"
    assert rep["totals"]["docs_new"] == 0
    assert any("collid" in s.lower() for s in rep["sources"][0]["error_samples"])


def test_a_productive_run_stays_ok_with_the_collision_listed(monkeypatch, tmp_path):
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _OneCollidingIssuerSource)
    rc, rep = _run_xbrl(monkeypatch, tmp_path, "0000320193,0000789019")
    assert rc == 0 and rep["outcome"] == "ok"
    assert rep["totals"]["docs_new"] == 1
    assert any("collid" in s.lower() for s in rep["sources"][0]["error_samples"])
