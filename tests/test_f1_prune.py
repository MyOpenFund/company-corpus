"""xbrl prunes its own stale F1 records (chantier 11, Backlog 18)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from company_corpus.config import Config, normalize_cik
from company_corpus.models import FilingRecord
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType

CIK = normalize_cik("320193")


def _rec(year: int, accession: str = "acc", frequency: str = "annual") -> FilingRecord:
    end = date(year, 12, 31)
    return FilingRecord(cik=CIK, form_type=FormType.F1, sec_form="10-K/XBRL",
                        accession=f"{accession}-{year}", filing_date=end + timedelta(days=50),
                        period_of_report=end, frequency=frequency, provenance="edgar_xbrl")


def _row(rec: FilingRecord, source: str = "sec") -> dict:
    return {"entity_id": CIK, "source": source, "period_end": rec.period_of_report.isoformat(),
            "frequency": rec.frequency, "basis": None, "accession": rec.accession,
            "concept": "assets", "value": 1}


def _seed(storage: Storage, *recs: FilingRecord) -> None:
    for r in recs:
        storage.write_financial_summary(r, f"<p>{r.doc_id}</p>", r.doc_id)
    storage.save_records(recs, dry_run=False)
    storage.write_financials_table(CIK, [_row(r) for r in recs])


def _table(config) -> list[dict]:
    path = config.financials_dir / f"{CIK}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_stale_records_are_the_f1_records_in_scope_the_run_did_not_produce(config):
    st = Storage(config)
    old, new = _rec(2019), _rec(2023)
    _seed(st, old, new)
    a1 = FilingRecord(cik=CIK, form_type=FormType.A1, sec_form="10-K", accession="a1",
                      filing_date=date(2024, 2, 1))
    st.save_records([a1], dry_run=False)
    stale, in_scope = st.stale_f1_records(CIK, [new.doc_id])
    assert [r.doc_id for r in stale] == [old.doc_id] and in_scope == 2
    stale, in_scope = st.stale_f1_records(CIK, [], since_year=2023, until_year=2023)
    assert [r.doc_id for r in stale] == [new.doc_id] and in_scope == 1


def test_a_vanished_period_loses_its_row_files_table_group_and_is_logged(config):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    files = [config.data_dir / gone.primary_path, config.data_dir / gone.text_path]
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False, run_id="r1")
    assert [r.doc_id for r in res.records] == [gone.doc_id] and not res.errors
    assert not any(f.exists() for f in files) and len(res.files) == 2
    assert set(st.load_manifest(CIK)) == {kept.doc_id}
    assert {r["period_end"] for r in _table(config)} == {"2023-12-31"} and res.table_groups == 1
    log = [json.loads(x) for x in config.f1_prune_log_path.read_text().splitlines()]
    assert [(x["doc_id"], x["run_id"], x["accession"]) for x in log] == [
        (gone.doc_id, "r1", gone.accession)]


def test_an_accession_change_keeps_the_period_table_group(config):
    st = Storage(config)
    old, new = _rec(2023, "old"), _rec(2023, "new")
    _seed(st, old, new)
    res = st.prune_f1(CIK, [old], [new], dry_run=False)
    assert res.table_groups == 0 and {r["period_end"] for r in _table(config)} == {"2023-12-31"}
    assert set(st.load_manifest(CIK)) == {new.doc_id}


def test_a_rendered_pdf_and_an_unpointed_canonical_file_go_too(config):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    pdf = config.data_dir / gone.primary_path.replace(".primary.html", ".pdf")
    pdf.write_bytes(b"%PDF")
    gone.pdf_path = None                   # the pointer was lost: the canonical name still finds it
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert not pdf.exists() and len(res.files) == 3


@pytest.mark.parametrize("pointer", ["raw/{cik}/A1/2024/{id}.primary.htm", "../{id}.primary.html",
                                     "raw/{cik}/F1/companyfacts.json"])
def test_a_path_outside_the_record_s_f1_artefacts_refuses_the_record(config, pointer):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    target = config.data_dir / pointer.format(cik=CIK, id=gone.doc_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("keep me")
    gone.primary_path = pointer.format(cik=CIK, id=gone.doc_id)
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert not res.records and res.errors[0]["source"] == "f1_prune"
    assert target.read_text() == "keep me" and gone.doc_id in st.load_manifest(CIK)


def test_other_sources_and_non_f1_rows_are_never_touched(config):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    st.write_financials_table(CIK, [_row(gone, source="other")])
    a1 = FilingRecord(cik=CIK, form_type=FormType.A1, sec_form="10-K", accession="a1",
                      filing_date=date(2024, 2, 1))
    st.save_records([a1], dry_run=False)
    st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert {(r["source"], r["period_end"]) for r in _table(config)} == {
        ("other", "2019-12-31"), ("sec", "2023-12-31")}
    assert a1.doc_id in st.load_manifest(CIK)


def test_a_table_left_empty_is_deleted(config):
    st = Storage(config)
    only = _rec(2019)
    _seed(st, only)
    st.prune_f1(CIK, [only], [], dry_run=False)
    assert not (config.financials_dir / f"{CIK}.jsonl").exists()
    assert (config.manifest_file(CIK)).read_text() == ""


def test_a_dry_run_counts_and_changes_nothing(config):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    before = {p: p.read_bytes() for p in config.data_dir.rglob("*") if p.is_file()}
    res = st.prune_f1(CIK, [gone], [kept], dry_run=True)
    assert len(res.records) == 1 and len(res.files) == 2 and res.table_groups == 1
    assert {p: p.read_bytes() for p in config.data_dir.rglob("*") if p.is_file()} == before


def test_a_failed_delete_keeps_the_row_and_a_rerun_finishes(config, monkeypatch):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    real_unlink = Path.unlink

    def failing(self, *a, **kw):
        if self.suffix == ".txt":
            raise PermissionError("locked")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", failing)
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert not res.records and "PermissionError" in res.errors[0]["error"]
    assert gone.doc_id in st.load_manifest(CIK)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    stale, _ = st.stale_f1_records(CIK, [kept.doc_id])
    res = st.prune_f1(CIK, stale, [kept], dry_run=False)
    assert [r.doc_id for r in res.records] == [gone.doc_id]
    assert not list((config.raw_dir / CIK / "F1").rglob(f"{gone.doc_id}.*"))


def test_the_prune_fraction_is_validated():
    with pytest.raises(ValueError, match="f1_prune_max_fraction"):
        Config(f1_prune_max_fraction=1.5)


def test_a_symlink_escaping_raw_cik_f1_refuses_the_record(config, tmp_path):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("keep me")
    link = config.data_dir / gone.text_path
    link.unlink()
    link.symlink_to(outside)               # named like the record, resolves elsewhere
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert not res.records and "refusing" in res.errors[0]["error"]
    assert outside.read_text() == "keep me" and gone.doc_id in st.load_manifest(CIK)
    assert (config.data_dir / gone.primary_path).exists()


def test_a_record_without_filing_date_is_pruned_from_unknown(config):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    gone.filing_date = None                # raw/<cik>/F1/unknown/
    _seed(st, gone, kept)
    assert "/F1/unknown/" in gone.primary_path
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert [r.doc_id for r in res.records] == [gone.doc_id] and len(res.files) == 2
    assert not list((config.raw_dir / CIK / "F1" / "unknown").glob("*"))
    assert set(st.load_manifest(CIK)) == {kept.doc_id}


def test_a_record_without_period_is_out_of_a_year_scope(config):
    st = Storage(config)
    undated = FilingRecord(cik=CIK, form_type=FormType.F1, sec_form="10-K/XBRL", accession="x",
                           filing_date=date(2024, 2, 1), frequency="annual")
    st.save_records([undated], dry_run=False)
    assert st.stale_f1_records(CIK, [], since_year=2000) == ([], 0)
    stale, in_scope = st.stale_f1_records(CIK, [])
    assert [r.doc_id for r in stale] == [undated.doc_id] and in_scope == 1


def test_a_refused_record_does_not_stop_the_others(config):
    st = Storage(config)
    bad, good, kept = _rec(2018), _rec(2019), _rec(2023)
    _seed(st, bad, good, kept)
    bad.primary_path = f"raw/{CIK}/F1/companyfacts.json"
    res = st.prune_f1(CIK, [bad, good], [kept], dry_run=False)
    assert [r.doc_id for r in res.records] == [good.doc_id] and len(res.errors) == 1
    assert set(st.load_manifest(CIK)) == {bad.doc_id, kept.doc_id}
    # the refused record's table group stays: its row is still in the manifest
    assert {r["period_end"] for r in _table(config)} == {"2018-12-31", "2023-12-31"}


def test_nothing_stale_writes_nothing(config):
    st = Storage(config)
    kept = _rec(2023)
    _seed(st, kept)
    res = st.prune_f1(CIK, [], [kept], dry_run=False)
    assert not res.records and not res.errors and res.table_groups == 0
    assert not config.f1_prune_log_path.exists()


def test_a_record_passed_as_both_stale_and_kept_is_refused(config):
    st = Storage(config)
    rec = _rec(2023)
    _seed(st, rec)
    res = st.prune_f1(CIK, [rec], [rec], dry_run=False)
    assert not res.records and "also keeps" in res.errors[0]["error"]
    assert (config.data_dir / rec.primary_path).exists() and rec.doc_id in st.load_manifest(CIK)
