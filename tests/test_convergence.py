"""discover -> download -> discover must converge, not destroy (DI-C1, Rob-C4, DI-C3)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from company_corpus import rag
from company_corpus.config import Config, normalize_cik
from company_corpus.models import FilingRecord
from company_corpus.pipeline import fetch_financials
from company_corpus.storage import ShrinkGuardError, Storage, group_key, merge_rows
from company_corpus.taxonomy import FormType


class _OneShotFetcher:
    """Serves the submission once; a second call is a test failure."""

    def __init__(self, body: str):
        self.body = body
        self.calls: list[str] = []

    def get_text(self, url: str, **_) -> str:
        self.calls.append(url)
        return self.body


def _discovery_record() -> FilingRecord:
    """What EdgarSubmissions.discover builds: every pointer empty."""
    return FilingRecord(
        cik="320193", form_type=FormType.A1, sec_form="10-K",
        accession="0000320193-24-000123", company="Apple Inc.",
        filing_date=date(2024, 11, 1),
        primary_doc_url="https://example.invalid/aapl-20240928.htm",
        submission_url="https://example.invalid/0000320193-24-000123.txt",
    )


def test_discover_download_discover_keeps_rag_items(config, sample_submission):
    st = Storage(config)
    st.save_records([_discovery_record()], dry_run=False)

    rec = next(iter(st.load_manifest("320193").values()))
    fetcher = _OneShotFetcher(sample_submission)
    assert st.fetch_and_store(rec, fetcher).status == "downloaded"
    st.save_records([rec], dry_run=False)

    st.save_records([_discovery_record()], dry_run=False)  # the second discovery

    kept = next(iter(st.load_manifest("320193").values()))
    assert kept.sha256 and kept.local_path and kept.primary_path and kept.text_path
    assert list(rag.iter_items(ciks=["320193"], config=config))


# ---------------------------------------------------------------------------
# A narrowed re-run must merge into the stored table, not truncate it (DI-C3,
# Rob-C8).
# ---------------------------------------------------------------------------

_KEY = group_key(("source", "period_end", "frequency", "basis"))


def _read(path: Path) -> list[dict]:
    """Read a JSONL table written by Storage into a list of dicts."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _row(period_end, concept, value, source="sec", basis=None):
    return {"entity_id": "0000320193", "source": source, "period_end": period_end,
            "frequency": "annual", "basis": basis, "kind": "reported",
            "concept": concept, "value": value, "unit": "USD"}


def test_a_narrowed_rerun_keeps_the_other_periods(config):
    st = Storage(config)
    st.write_financials_table("320193", [_row("2019-12-31", "assets", 1),
                                         _row("2020-12-31", "assets", 2),
                                         _row("2021-12-31", "assets", 3)])
    st.write_financials_table("320193", [_row("2021-12-31", "assets", 30)])
    rows = _read(config.financials_dir / "0000320193.jsonl")
    assert [r["period_end"] for r in rows] == ["2019-12-31", "2020-12-31", "2021-12-31"]
    assert rows[-1]["value"] == 30


def test_a_period_is_replaced_wholesale_not_row_by_row(config):
    st = Storage(config)
    st.write_financials_table("320193", [_row("2021-12-31", "assets", 1),
                                         _row("2021-12-31", "inventory", 5)])
    st.write_financials_table("320193", [_row("2021-12-31", "assets", 2)])
    rows = _read(config.financials_dir / "0000320193.jsonl")
    # The concept the new vintage dropped must NOT survive as a stale row.
    assert [r["concept"] for r in rows] == ["assets"]


def test_two_bases_of_one_period_coexist(config):
    st = Storage(config)
    st.write_register_financials_table("123456789", [
        _row("2021-12-31", "assets", 1, source="brreg", basis="company"),
        _row("2021-12-31", "assets", 2, source="brreg", basis="consolidated")])
    st.write_register_financials_table("123456789", [
        _row("2021-12-31", "assets", 9, source="brreg", basis="company")])
    rows = _read(config.financials_register_dir / "123456789.jsonl")
    assert sorted((r["basis"], r["value"]) for r in rows) == [
        ("company", 9), ("consolidated", 2)]


def test_two_identical_form4_lines_both_survive(config):
    st = Storage(config)
    line = {"cik": "0000320193", "accession": "acc-1", "doc_type": "E1",
            "shares": 100, "price": 1.0}
    st.write_ownership_table("320193", [dict(line), dict(line)])
    st.write_ownership_table("320193", [dict(line), dict(line)])
    assert len(_read(config.ownership_dir / "0000320193.jsonl")) == 2


def test_replace_tables_reproduces_the_old_truncation(config):
    st = Storage(config)
    st.write_financials_table("320193", [_row("2019-12-31", "assets", 1),
                                         _row("2020-12-31", "assets", 2)])
    replacing = Storage(Config(data_dir=config.data_dir, replace_tables=True,
                               no_shrink_fraction=1.0))
    replacing.write_financials_table("320193", [_row("2020-12-31", "assets", 2)])
    assert len(_read(config.financials_dir / "0000320193.jsonl")) == 1


def test_the_no_shrink_guard_refuses_a_replace_that_loses_a_period(config):
    st = Storage(config)
    st.write_financials_table("320193", [_row("2019-12-31", "assets", 1),
                                         _row("2020-12-31", "assets", 2)])
    replacing = Storage(Config(data_dir=config.data_dir, replace_tables=True))
    with pytest.raises(ShrinkGuardError) as excinfo:
        replacing.write_financials_table("320193", [_row("2020-12-31", "assets", 2)])
    # The remedy must keep --replace: --allow-shrink alone would merge silently
    # instead of performing the rebuild the operator asked for.
    assert "--replace --allow-shrink" in str(excinfo.value)


def test_dropping_rows_inside_a_kept_group_is_not_a_shrink(config):
    """The guard counts groups, not rows: a period that reports fewer concepts
    this vintage is a normal replacement, not a loss of history."""
    st = Storage(config)
    st.write_financials_table("320193", [_row("2021-12-31", "assets", 1),
                                         _row("2021-12-31", "inventory", 5)])
    replacing = Storage(Config(data_dir=config.data_dir, replace_tables=True,
                               no_shrink_fraction=0.0))
    replacing.write_financials_table("320193", [_row("2021-12-31", "assets", 2)])
    rows = _read(config.financials_dir / "0000320193.jsonl")
    assert [(r["concept"], r["value"]) for r in rows] == [("assets", 2)]


# ---------------------------------------------------------------------------
# --replace is scoped to the RUN, not to the write: several producers write one
# entity's table many times in a single run (one CH zip member, one LU yearly
# file, ...), and replacement "by the current run" must not mean "by whichever
# member wrote last".
# ---------------------------------------------------------------------------
def test_replace_applies_once_per_run_not_once_per_write(config):
    replacing = Storage(Config(data_dir=config.data_dir, replace_tables=True))
    replacing.write_financials_table("320193", [_row("2019-12-31", "assets", 1)])
    replacing.write_financials_table("320193", [_row("2020-12-31", "assets", 2)])
    rows = _read(config.financials_dir / "0000320193.jsonl")
    assert [r["period_end"] for r in rows] == ["2019-12-31", "2020-12-31"]


def test_replace_drops_the_stored_table_once_then_merges(config):
    st = Storage(config)
    st.write_financials_table("320193", [_row("2018-12-31", "assets", 0)])
    replacing = Storage(Config(data_dir=config.data_dir, replace_tables=True,
                               no_shrink_fraction=1.0))
    replacing.write_financials_table("320193", [_row("2019-12-31", "assets", 1)])
    replacing.write_financials_table("320193", [_row("2020-12-31", "assets", 2)])
    rows = _read(config.financials_dir / "0000320193.jsonl")
    # The pre-run vintage is gone (the run replaced), both of the run's own
    # writes survive (the run merged with itself).
    assert [r["period_end"] for r in rows] == ["2019-12-31", "2020-12-31"]


def test_replace_is_tracked_per_table(config):
    """One entity's replacement must not spend another entity's."""
    st = Storage(config)
    st.write_financials_table("320193", [_row("2018-12-31", "assets", 0)])
    st.write_financials_table("789019", [_row("2018-12-31", "assets", 0)])
    replacing = Storage(Config(data_dir=config.data_dir, replace_tables=True,
                               no_shrink_fraction=1.0))
    replacing.write_financials_table("320193", [_row("2019-12-31", "assets", 1)])
    replacing.write_financials_table("789019", [_row("2019-12-31", "assets", 2)])
    for cik in ("0000320193", "0000789019"):
        rows = _read(config.financials_dir / f"{cik}.jsonl")
        assert [r["period_end"] for r in rows] == ["2019-12-31"]


def test_ownership_replace_is_also_once_per_run(config):
    replacing = Storage(Config(data_dir=config.data_dir, replace_tables=True))
    replacing.write_ownership_table("320193", [{"cik": "0000320193", "accession": "acc-1"}])
    replacing.write_ownership_table("320193", [{"cik": "0000320193", "accession": "acc-2"}])
    rows = _read(config.ownership_dir / "0000320193.jsonl")
    assert [r["accession"] for r in rows] == ["acc-1", "acc-2"]


def test_a_new_run_replaces_again(config):
    """The scope is the Storage instance: the next run gets its own replacement."""
    first = Storage(Config(data_dir=config.data_dir, replace_tables=True))
    first.write_financials_table("320193", [_row("2019-12-31", "assets", 1)])
    second = Storage(Config(data_dir=config.data_dir, replace_tables=True,
                            no_shrink_fraction=1.0))
    second.write_financials_table("320193", [_row("2020-12-31", "assets", 2)])
    rows = _read(config.financials_dir / "0000320193.jsonl")
    assert [r["period_end"] for r in rows] == ["2020-12-31"]


# ---------------------------------------------------------------------------
# A tripped guard is one issuer's problem, not the run's (like DI-C2).
# ---------------------------------------------------------------------------
class _OneSummarySource:
    """EdgarXBRL stand-in: one clean annual summary for every issuer."""

    def __init__(self, **_):
        self.errors: list[dict] = []

    def period_summaries(self, cik, **_):
        from company_corpus.financials import PeriodSummary
        return {"facts": {}}, [PeriodSummary(
            period_end=date(2024, 9, 30), frequency="annual",
            publication_date=date(2024, 11, 1), sec_form="10-K",
            accession=f"acc-{normalize_cik(cik)}", company="Acme",
            company_current="Acme",
            values={"assets": {"value": 1, "unit": "USD", "label": "Assets"}})]


class _RefusingStorage(Storage):
    """Trips the no-shrink guard on one issuer's financials table."""

    def __init__(self, config, *, refuse_for: str):
        super().__init__(config)
        self.refuse_for = normalize_cik(refuse_for)

    def write_financials_table(self, cik, rows):
        if normalize_cik(cik) == self.refuse_for:
            raise ShrinkGuardError(f"{cik}: refusing to drop 2 of 2 record group(s)")
        return super().write_financials_table(cik, rows)


def test_a_shrink_guard_error_is_a_per_issuer_report_error(config, monkeypatch):
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _OneSummarySource)
    storage = _RefusingStorage(config, refuse_for="320193")
    report = fetch_financials(["320193", "789019"], dry_run=False, config=config,
                              fetcher=object(), storage=storage)
    assert [e["context"] for e in report.errors] == [normalize_cik("320193")]
    assert "refusing to drop" in report.errors[0]["error"]
    # The refused issuer's remaining writes are skipped...
    assert not Storage(config).load_manifest("320193")
    # ...and the run's other issuers are untouched by it.
    assert len(Storage(config).load_manifest("789019")) == 1
    assert normalize_cik("320193") in config.discovery_errors_path.read_text(encoding="utf-8")


def test_merge_is_stable_in_order():
    merged = merge_rows([_row("2019-12-31", "a", 1), _row("2021-12-31", "a", 3)],
                        [_row("2020-12-31", "a", 2), _row("2019-12-31", "a", 9)],
                        key=_KEY)
    assert [r["period_end"] for r in merged] == ["2019-12-31", "2021-12-31", "2020-12-31"]
