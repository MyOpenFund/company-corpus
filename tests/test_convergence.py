"""discover -> download -> discover must converge, not destroy (DI-C1, Rob-C4, DI-C3)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from company_corpus import rag
from company_corpus.config import Config
from company_corpus.models import FilingRecord
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
    assert "--allow-shrink" in str(excinfo.value)


def test_merge_is_stable_in_order():
    merged = merge_rows([_row("2019-12-31", "a", 1), _row("2021-12-31", "a", 3)],
                        [_row("2020-12-31", "a", 2), _row("2019-12-31", "a", 9)],
                        key=_KEY)
    assert [r["period_end"] for r in merged] == ["2019-12-31", "2021-12-31", "2020-12-31"]
