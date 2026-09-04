from __future__ import annotations

from datetime import date

import pytest

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
