from __future__ import annotations

from datetime import date

from company_corpus.pipeline import fetch_financials
from company_corpus.sources.edgar_xbrl import EdgarXBRL
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType


def test_period_summaries_from_companyfacts(xbrl_fetcher, config):
    src = EdgarXBRL(fetcher=xbrl_fetcher, config=config)
    facts, summaries = src.period_summaries("320193")
    assert facts is not None
    assert {(s.fy, s.frequency) for s in summaries} == {(2023, "annual"), (2023, "quarterly")}
    assert not src.errors


def test_missing_companyfacts_records_error(make_fetcher, config):
    src = EdgarXBRL(fetcher=make_fetcher({}), config=config)
    facts, summaries = src.period_summaries("999999")
    assert facts is None and summaries == []
    assert len(src.errors) == 1 and src.errors[0]["source"] == "edgar_xbrl"


def test_fetch_financials_writes_records_and_artifacts(xbrl_fetcher, config):
    rep = fetch_financials(["320193"], dry_run=False, config=config, fetcher=xbrl_fetcher)
    assert rep.issuers == 1
    assert rep.periods == 2
    assert rep.stats.added == 2

    st = Storage(config)
    manifest = st.load_manifest("320193")
    assert all(r.form_type is FormType.F1 for r in manifest.values())
    fy = next(r for r in manifest.values() if r.sec_form.startswith("10-K"))
    assert fy.filing_date == date(2023, 11, 1)        # publication date
    assert fy.period_of_report == date(2023, 9, 30)
    assert fy.primary_path and (config.data_dir / fy.primary_path).exists()
    assert fy.text_path and (config.data_dir / fy.text_path).exists()

    # Canonical raw facts + normalized table persisted.
    assert (config.data_dir / "raw" / "0000320193" / "F1" / "companyfacts.json").exists()
    assert (config.financials_dir / "0000320193.jsonl").exists()


def test_fetch_financials_dry_run_writes_nothing(xbrl_fetcher, config):
    rep = fetch_financials(["320193"], dry_run=True, config=config, fetcher=xbrl_fetcher)
    assert rep.periods == 2 and rep.stats.added == 2
    assert not (config.data_dir / "manifest").exists()
    assert not config.financials_dir.exists()


def test_fetch_financials_is_idempotent(xbrl_fetcher, config):
    fetch_financials(["320193"], dry_run=False, config=config, fetcher=xbrl_fetcher)
    rep = fetch_financials(["320193"], dry_run=False, config=config, fetcher=xbrl_fetcher)
    assert rep.stats.added == 0 and rep.stats.unchanged == 2
