from __future__ import annotations

import json
from datetime import date

import pytest

from company_corpus.config import Config
from company_corpus.models import FilingRecord
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType


def _rec(accession="0000320193-24-000123", **kw):
    base = dict(
        cik="320193",
        form_type=FormType.A1,
        sec_form="10-K",
        accession=accession,
        company="Apple Inc.",
        filing_date=date(2024, 11, 1),
    )
    base.update(kw)
    return FilingRecord(**base)


def test_dry_run_writes_nothing(config):
    st = Storage(config)
    stats = st.save_records([_rec()], dry_run=True)
    assert stats.added == 1
    assert not config.manifest_file("320193").exists()


def test_write_persists_and_roundtrips(config):
    st = Storage(config)
    st.save_records([_rec()], dry_run=False)
    path = config.manifest_file("320193")
    assert path.exists()
    loaded = st.load_manifest("320193")
    assert len(loaded) == 1
    rec = next(iter(loaded.values()))
    assert rec.form_type is FormType.A1
    assert rec.filing_date == date(2024, 11, 1)


def test_idempotent_resave_is_unchanged(config):
    st = Storage(config)
    st.save_records([_rec()], dry_run=False)
    stats = st.save_records([_rec()], dry_run=False)
    assert stats.added == 0 and stats.updated == 0 and stats.unchanged == 1


def test_update_in_place_on_metadata_change(config):
    st = Storage(config)
    st.save_records([_rec()], dry_run=False)
    # Same doc_id (cik|form|accession) but corrected date -> update, not duplicate.
    stats = st.save_records([_rec(filing_date=date(2024, 11, 2))], dry_run=False)
    assert stats.updated == 1
    loaded = st.load_manifest("320193")
    assert len(loaded) == 1
    assert next(iter(loaded.values())).filing_date == date(2024, 11, 2)


def test_distinct_accessions_coexist(config):
    st = Storage(config)
    st.save_records([_rec(), _rec(accession="0000320193-23-000106")], dry_run=False)
    assert len(st.load_manifest("320193")) == 2


def test_record_errors_appends(config):
    st = Storage(config)
    n = st.record_errors([{"source": "x", "context": "c", "url": "u", "error": "boom"}])
    assert n == 1
    assert config.discovery_errors_path.exists()


def test_write_leaves_no_tmp_file(config):
    st = Storage(config)
    st.save_records([_rec()], dry_run=False)
    path = config.manifest_file("320193")
    # The atomic write must not leave the staging sibling behind.
    assert not path.with_name(path.name + ".tmp").exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_load_manifest_skips_corrupt_line(config):
    st = Storage(config)
    st.save_records([_rec(), _rec(accession="0000320193-23-000106")], dry_run=False)
    path = config.manifest_file("320193")
    # Simulate a truncated/garbled row from an interrupted legacy write.
    good = path.read_text(encoding="utf-8").splitlines()
    path.write_text(good[0] + "\n{ this is not json\n" + good[1] + "\n", encoding="utf-8")
    with pytest.warns(UserWarning, match="unparseable manifest row"):
        loaded = st.load_manifest("320193")
    # The two valid rows survive; only the corrupt one is dropped.
    assert len(loaded) == 2


def test_rediscovery_preserves_download_pointers(config):
    st = Storage(config)
    stored = _rec(local_path="raw/a.txt", sha256="abc", primary_path="raw/a.htm",
                  text_path="raw/a.txt", pdf_path="raw/a.pdf")
    st.save_records([stored], dry_run=False)
    # A fresh discovery record: same doc_id, every pointer empty by construction.
    stats = st.save_records([_rec(title="corrected")], dry_run=False)
    kept = next(iter(st.load_manifest("320193").values()))
    assert (kept.local_path, kept.sha256, kept.primary_path, kept.text_path,
            kept.pdf_path) == ("raw/a.txt", "abc", "raw/a.htm", "raw/a.txt", "raw/a.pdf")
    assert kept.title == "corrected"
    assert stats.updated == 1


def test_sticky_carry_forward_does_not_mask_a_real_change(config):
    st = Storage(config)
    st.save_records([_rec(local_path="raw/a.txt", sha256="abc")], dry_run=False)
    # A record that re-derives an artefact must WIN; sticky only fills blanks.
    st.save_records([_rec(local_path="raw/b.txt", sha256="")], dry_run=False)
    kept = next(iter(st.load_manifest("320193").values()))
    assert kept.local_path == "raw/b.txt"
    assert kept.sha256 == "abc"


def test_identical_rediscovery_counts_unchanged_not_updated(config):
    st = Storage(config)
    st.save_records([_rec(local_path="raw/a.txt", sha256="abc")], dry_run=False)
    stats = st.save_records([_rec()], dry_run=False)
    assert (stats.updated, stats.unchanged) == (0, 1)


# ---- the read-merge-write table core (chantier 3, task 5) ----

def _fin_row(period_end: str, concept: str, value: int) -> dict:
    return {"entity_id": "0000320193", "source": "sec", "period_end": period_end,
            "frequency": "annual", "basis": None, "kind": "reported",
            "concept": concept, "value": value, "unit": "USD"}


def test_a_corrupt_stored_line_is_skipped_not_fatal(config):
    """One truncated line must not turn a merge into a full-table replacement."""
    st = Storage(config)
    st.write_financials_table("320193", [_fin_row("2019-12-31", "assets", 1)])
    path = config.financials_dir / "0000320193.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + '{"entity_id": "032019\n',
                    encoding="utf-8")
    with pytest.warns(UserWarning, match="unparseable table row"):
        st.write_financials_table("320193", [_fin_row("2020-12-31", "assets", 2)])
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [r["period_end"] for r in rows] == ["2019-12-31", "2020-12-31"]


def test_a_batch_repeating_a_group_keeps_both_of_its_rows(config):
    """Two rows sharing a natural key inside ONE batch are one group, not a dedupe."""
    st = Storage(config)
    st.write_financials_table("320193", [_fin_row("2021-12-31", "assets", 1),
                                         _fin_row("2021-12-31", "assets", 1)])
    path = config.financials_dir / "0000320193.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 2


def test_the_shrink_guard_tolerates_the_configured_fraction(config):
    """no_shrink_fraction is a knob, not a boolean: 0.5 lets a half-table go."""
    st = Storage(config)
    st.write_financials_table("320193", [_fin_row("2019-12-31", "assets", 1),
                                         _fin_row("2020-12-31", "assets", 2)])
    lenient = Storage(Config(data_dir=config.data_dir, replace_tables=True,
                             no_shrink_fraction=0.5))
    lenient.write_financials_table("320193", [_fin_row("2020-12-31", "assets", 2)])
    path = config.financials_dir / "0000320193.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_a_non_sticky_field_is_still_clearable(config):
    """Stickiness covers the artefact pointers and NOTHING else (DI-C1, T1).

    The carry-forward exists because a fresh discovery record has empty
    pointers and must not orphan the bytes on disk. Applied to every field it
    would be the opposite bug: a source that corrects a value to blank -- an
    EDGAR period_of_report withdrawn, a title that turns out to be empty --
    could never be recorded, and the manifest would keep a value its source no
    longer asserts.
    """
    st = Storage(config)
    st.save_records([_rec(period_of_report=date(2024, 9, 28), company="Old Name",
                          local_path="raw/a.txt", sha256="abc")], dry_run=False)

    st.save_records([_rec(period_of_report=None, company="")], dry_run=False)

    kept = next(iter(st.load_manifest("320193").values()))
    assert kept.period_of_report is None and kept.company == ""
    assert (kept.local_path, kept.sha256) == ("raw/a.txt", "abc"), \
        "the pointers, and only the pointers, survive"
