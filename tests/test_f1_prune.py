"""xbrl prunes its own stale F1 records (chantier 11, Backlog 18)."""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from company_corpus.config import Config, normalize_cik
from company_corpus.financials import PeriodSummary
from company_corpus.models import FilingRecord
from company_corpus.pipeline import fetch_financials
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType
from company_corpus.verify import verify

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
    # the primary went before the .txt failed: reported; the row stays, so does its table group
    assert res.files == [gone.primary_path] and res.table_groups == 0
    assert {r["period_end"] for r in _table(config)} == {"2019-12-31", "2023-12-31"}
    assert not config.f1_prune_log_path.exists()
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
    outside = tmp_path / f"{gone.doc_id}.txt"   # the record's name: only the boundary refuses it
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


# ---- B1 review fixes ----


def test_a_record_of_another_issuer_is_refused(config):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    res = st.prune_f1("789019", [gone], [kept], dry_run=False)
    assert not res.records and "789019" in res.errors[0]["context"]
    assert "cik" in res.errors[0]["error"] and gone.doc_id in st.load_manifest(CIK)
    assert (config.data_dir / gone.primary_path).exists()


def test_a_refused_record_keeps_a_group_it_shares_with_a_pruned_sibling(config):
    st = Storage(config)
    bad, good, kept = _rec(2019, "bad"), _rec(2019, "good"), _rec(2023)
    _seed(st, bad, good, kept)
    bad.primary_path = f"raw/{CIK}/F1/companyfacts.json"
    res = st.prune_f1(CIK, [bad, good], [kept], dry_run=False)
    assert [r.doc_id for r in res.records] == [good.doc_id] and res.table_groups == 0
    assert {r["period_end"] for r in _table(config)} == {"2019-12-31", "2023-12-31"}


def test_a_kept_record_not_yet_in_the_manifest_keeps_its_group(config):
    st = Storage(config)
    old = _rec(2023, "old")
    _seed(st, old)
    new = _rec(2023, "new")                 # a dry run: the run's record is not saved
    res = st.prune_f1(CIK, [old], [new], dry_run=True)
    assert len(res.records) == 1 and res.table_groups == 0


def test_a_stale_record_already_gone_from_the_manifest_is_skipped(config):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    st.prune_f1(CIK, [gone], [kept], dry_run=False)
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert not res.records and not res.errors and not res.files and res.table_groups == 0
    assert len(config.f1_prune_log_path.read_text().splitlines()) == 1
    assert st.prune_f1(CIK, [gone], [kept], dry_run=True).records == []


def test_a_symlink_to_another_of_its_artefacts_goes_with_its_target(config):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    link = config.data_dir / gone.text_path
    target = config.data_dir / gone.primary_path
    link.unlink()
    link.symlink_to(target.name)            # <doc_id>.txt -> <doc_id>.primary.html, same dir
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert [r.doc_id for r in res.records] == [gone.doc_id] and not res.errors
    assert not link.is_symlink() and not link.exists() and not target.exists()
    assert sorted(res.files) == sorted([gone.primary_path, gone.text_path])


def test_a_symlink_to_a_file_not_named_after_the_record_refuses_it(config):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    link = config.data_dir / gone.text_path
    other = link.parent / "someone-else.txt"
    other.write_text("keep me")
    link.unlink()
    link.symlink_to(other.name)
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert not res.records and "refusing" in res.errors[0]["error"]
    assert link.is_symlink() and other.read_text() == "keep me"


def test_a_quarterly_record_sharing_an_annual_period_end_drops_its_group_only(config):
    st = Storage(config)
    annual, quarterly = _rec(2023), _rec(2023, frequency="quarterly")
    _seed(st, annual, quarterly)
    res = st.prune_f1(CIK, [quarterly], [annual], dry_run=False)
    assert [r.doc_id for r in res.records] == [quarterly.doc_id] and res.table_groups == 1
    assert {(r["period_end"], r["frequency"]) for r in _table(config)} == {
        ("2023-12-31", "annual")}
    assert set(st.load_manifest(CIK)) == {annual.doc_id}


def test_a_semi_annual_record_is_pruned_like_any_other(config):
    st = Storage(config)
    annual, semi = _rec(2023), _rec(2023, frequency="semi-annual")
    semi.period_of_report = date(2023, 6, 30)
    _seed(st, annual, semi)
    res = st.prune_f1(CIK, [semi], [annual], dry_run=False)
    assert [r.doc_id for r in res.records] == [semi.doc_id] and res.table_groups == 1
    assert {(r["period_end"], r["frequency"]) for r in _table(config)} == {
        ("2023-12-31", "annual")}


def test_the_log_line_carries_every_field(config):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    st.prune_f1(CIK, [gone], [kept], dry_run=False, run_id="r9")
    (line,) = [json.loads(x) for x in config.f1_prune_log_path.read_text().splitlines()]
    ts = line.pop("ts")
    assert ts.startswith(str(date.today().year)) and "+00:00" in ts
    assert line == {"run_id": "r9", "cik": CIK, "doc_id": gone.doc_id,
                    "period_of_report": "2019-12-31", "frequency": "annual",
                    "accession": gone.accession, "filing_date": gone.filing_date.isoformat(),
                    "sha256": gone.sha256, "deleted_files": [gone.primary_path, gone.text_path]}


def test_a_relative_data_dir_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = Config(data_dir=Path("data"), contact="test@example.com")
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert sorted(res.files) == sorted([gone.primary_path, gone.text_path])
    assert not (tmp_path / "data" / gone.primary_path).exists()
    assert set(st.load_manifest(CIK)) == {kept.doc_id}


def test_a_data_dir_reached_through_a_symlink_logs_clean_paths(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "via").symlink_to(real)
    config = Config(data_dir=tmp_path / "via", contact="test@example.com")
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert sorted(res.files) == sorted([gone.primary_path, gone.text_path])
    assert all(f.startswith(f"raw/{CIK}/F1/") for f in res.files)
    (line,) = [json.loads(x) for x in config.f1_prune_log_path.read_text().splitlines()]
    assert line["deleted_files"] == [gone.primary_path, gone.text_path]
    assert not (real / gone.primary_path).exists()


# ---- B2: fetch_financials prunes after a clean read ----



def _summary(year: int, accession: str = "acc") -> PeriodSummary:
    return PeriodSummary(period_end=date(year, 12, 31), frequency="annual",
                         publication_date=date(year + 1, 2, 20), sec_form="10-K",
                         accession=f"{accession}-{year}", company="Acme", company_current="Acme",
                         values={"assets": {"value": year, "unit": "USD", "label": "Assets"}})


class _Source:
    """EdgarXBRL stand-in: ``plan[cik]`` = facts, summaries, error items, coverage."""

    plan: dict = {}

    def __init__(self, **_):
        self.errors: list[dict] = []
        self.coverage: Counter = Counter()

    def period_summaries(self, cik, since_year=None, until_year=None):
        facts, summaries, errors, coverage = self.plan[normalize_cik(cik)]
        self.errors.extend(errors)
        self.coverage.update(coverage)
        return facts, [s for s in summaries
                       if (since_year is None or s.period_end.year >= since_year)
                       and (until_year is None or s.period_end.year <= until_year)]


FACTS = {"cik": 320193, "facts": {"us-gaap": {}}}
OTHER_FACTS = {"cik": 789019, "facts": {"us-gaap": {}}}
TEN = [_summary(y) for y in range(2014, 2024)]


def _run(config, monkeypatch, summaries, *, cik=CIK, facts=FACTS, errors=(), coverage=None,
         **kw):
    _Source.plan = {**_Source.plan, cik: (facts, list(summaries), list(errors), coverage or {})}
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _Source)
    kw.setdefault("dry_run", False)
    return fetch_financials([cik], config=config, fetcher=object(), **kw)


@pytest.fixture(autouse=True)
def _fresh_plan():
    _Source.plan = {}


def _f1_ids(config, cik=CIK) -> set[str]:
    return {i for i, r in Storage(config).load_manifest(cik).items() if r.form_type is FormType.F1}


def test_one_changed_accession_in_ten_is_pruned_at_the_guard_boundary(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    before = _f1_ids(config)
    rep = _run(config, monkeypatch, [*TEN[:-1], _summary(2023, "new")])
    assert rep.pruned == 1 and rep.prune_refused == 0 and rep.pruned_files == 2
    assert rep.pruned_table_groups == 0 and rep.prune_by_issuer == {CIK: 1}
    assert len(_f1_ids(config)) == 10 and len(before - _f1_ids(config)) == 1
    assert verify(config) == []


def test_two_of_ten_is_refused_without_allow_shrink(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    rep = _run(config, monkeypatch, TEN[2:])
    assert rep.pruned == 0 and rep.prune_refused == 1 and len(_f1_ids(config)) == 10
    assert any(e["source"] == "f1_prune" and "--allow-shrink" in e["error"] for e in rep.errors)
    config.f1_prune_max_fraction = 1.0
    rep = _run(config, monkeypatch, TEN[2:])
    assert rep.pruned == 2 and rep.pruned_table_groups == 2 and len(_f1_ids(config)) == 8


def test_an_issuer_with_no_summary_left_is_pruned_whole_with_allow_shrink(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    facts_file = config.raw_dir / CIK / "F1" / "companyfacts.json"
    assert facts_file.exists()
    rep = _run(config, monkeypatch, [])
    assert rep.prune_refused == 1 and len(_f1_ids(config)) == 10
    config.f1_prune_max_fraction = 1.0
    rep = _run(config, monkeypatch, [])
    assert rep.pruned == 10 and _f1_ids(config) == set()
    assert not (config.financials_dir / f"{CIK}.jsonl").exists() and facts_file.exists()
    assert verify(config) == []


@pytest.mark.parametrize("kind", ["fetch_error", "source_error", "statements_missing"])
def test_an_issuer_not_read_cleanly_is_never_pruned(config, monkeypatch, kind):
    _run(config, monkeypatch, TEN)
    config.f1_prune_max_fraction = 1.0
    error = {"source": "edgar_xbrl", "context": CIK, "error": "boom"}
    kw = {"fetch_error": {"facts": None, "errors": [error]},
          "source_error": {"errors": [error]},
          "statements_missing": {"coverage": {"statements_missing": 1}}}[kind]
    rep = _run(config, monkeypatch, [], **kw)
    assert rep.prune_skipped == 1 and rep.pruned == 0 and len(_f1_ids(config)) == 10


def test_missing_statements_may_be_allowed(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    config.f1_prune_requires_statements = False
    rep = _run(config, monkeypatch, TEN[1:], coverage={"statements_missing": 1})
    assert rep.pruned == 1


def test_a_failed_statements_refresh_skips_every_issuer(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    monkeypatch.setattr("company_corpus.pipeline.refresh_statements",
                        lambda *a, **k: SimpleNamespace(errors=["zip unreadable"]))
    rep = _run(config, monkeypatch, TEN[1:])
    assert rep.prune_skipped == 1 and len(_f1_ids(config)) == 10


def test_a_colliding_issuer_is_never_pruned(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    rep = _run(config, monkeypatch, [TEN[0], TEN[0]])
    assert rep.prune_skipped == 1 and len(_f1_ids(config)) == 10


def test_years_scope_protects_the_periods_the_run_did_not_look_at(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    rep = _run(config, monkeypatch, TEN[1:], since_year=2023, until_year=2023)
    assert rep.pruned == 0 and rep.prune_refused == 0 and len(_f1_ids(config)) == 10


def test_a_dry_run_counts_would_prune_and_writes_nothing(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    rep = _run(config, monkeypatch, TEN[1:], dry_run=True)
    assert rep.would_prune == 1 and rep.pruned == 0 and rep.pruned_files == 2
    assert len(_f1_ids(config)) == 10 and not config.f1_prune_log_path.exists()


def test_no_prune_leaves_everything(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    rep = _run(config, monkeypatch, TEN[1:], prune=False)
    assert rep.prune_summary["pruned"] == 0 and rep.prune_skipped == 0
    assert len(_f1_ids(config)) == 10


def test_one_refused_issuer_does_not_stop_the_others(config, monkeypatch):
    other = normalize_cik("789019")
    _run(config, monkeypatch, TEN)
    _run(config, monkeypatch, TEN, cik=other, facts=OTHER_FACTS)
    _Source.plan = {CIK: (FACTS, [], [], {}), other: (OTHER_FACTS, TEN[1:], [], {})}
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _Source)
    rep = fetch_financials([CIK, other], config=config, fetcher=object(), dry_run=False)
    assert rep.prune_refused == 1 and rep.prune_by_issuer == {other: 1}


def test_an_os_error_from_prune_f1_is_one_issuer_s_error(config, monkeypatch):
    other = normalize_cik("789019")
    _run(config, monkeypatch, TEN)
    _run(config, monkeypatch, TEN, cik=other, facts=OTHER_FACTS)
    real = Storage.prune_f1

    def failing(self, cik, *a, **kw):
        if normalize_cik(cik) == CIK:
            raise PermissionError("manifest locked")
        return real(self, cik, *a, **kw)

    monkeypatch.setattr(Storage, "prune_f1", failing)
    _Source.plan = {CIK: (FACTS, TEN[1:], [], {}), other: (OTHER_FACTS, TEN[1:], [], {})}
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _Source)
    rep = fetch_financials([CIK, other], config=config, fetcher=object(), dry_run=False)
    errs = [e for e in rep.errors if e["source"] == "f1_prune"]
    assert len(errs) == 1 and errs[0]["context"] == CIK and "PermissionError" in errs[0]["error"]
    assert rep.pruned == 1 and rep.prune_by_issuer == {other: 1}
    assert len(_f1_ids(config)) == 10 and len(_f1_ids(config, other)) == 9
    logged = [json.loads(x) for x in config.discovery_errors_path.read_text().splitlines()]
    assert any(e.get("source") == "f1_prune" and e.get("context") == CIK for e in logged)


def test_prune_f1_error_items_reach_the_report(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    m = Storage(config).load_manifest(CIK)
    gone = next(r for r in m.values() if r.period_of_report == date(2014, 12, 31))
    gone.primary_path = f"raw/{CIK}/F1/companyfacts.json"   # a pointer prune_f1 refuses
    Storage(config)._write_manifest(CIK, m.values())
    rep = _run(config, monkeypatch, TEN[1:])
    assert rep.pruned == 0 and any(
        e["source"] == "f1_prune" and e.get("doc_id") == gone.doc_id for e in rep.errors)
    assert (config.raw_dir / CIK / "F1" / "companyfacts.json").exists()


def test_a_crash_before_the_manifest_write_is_finished_by_the_next_run(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    gone = next(r for r in Storage(config).load_manifest(CIK).values()
                if r.period_of_report == date(2014, 12, 31))
    real = Storage._write_manifest
    calls = {"n": 0}

    def crashing(self, cik, records):
        records = list(records)
        if gone.doc_id not in {r.doc_id for r in records}:   # the prune's rewrite only
            calls["n"] += 1
            raise OSError("disk full")
        return real(self, cik, records)

    monkeypatch.setattr(Storage, "_write_manifest", crashing)
    rep = _run(config, monkeypatch, TEN[1:])
    assert rep.pruned == 0 and any(
        e["source"] == "f1_prune" and "disk full" in e["error"] for e in rep.errors)
    assert calls["n"] == 1 and gone.doc_id in _f1_ids(config)
    assert not (config.data_dir / gone.primary_path).exists()
    assert not config.f1_prune_log_path.exists()
    monkeypatch.setattr(Storage, "_write_manifest", real)
    rep = _run(config, monkeypatch, TEN[1:])
    assert rep.pruned == 1 and gone.doc_id not in _f1_ids(config)
    (line,) = [json.loads(x) for x in config.f1_prune_log_path.read_text().splitlines()]
    assert line["doc_id"] == gone.doc_id and line["deleted_files"] == []
    assert verify(config) == []


# ---- B2 review fixes ----


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("fraction,changed,pruned", [
    (0.1, 1, True), (0.1, 2, False), (0.18, 2, False), (0.2, 2, True)])
def test_the_guard_decides_alike_in_a_dry_run_and_a_write(config, monkeypatch, dry_run,
                                                          fraction, changed, pruned):
    _run(config, monkeypatch, TEN)
    config.f1_prune_max_fraction = fraction
    run = [*TEN[:-changed], *(_summary(s.period_end.year, "new") for s in TEN[-changed:])]
    rep = _run(config, monkeypatch, run, dry_run=dry_run)
    done = rep.would_prune if dry_run else rep.pruned
    assert (done, rep.prune_refused) == ((changed, 0) if pruned else (0, 1))


@pytest.mark.parametrize("dry_run", [True, False])
def test_the_guard_base_is_the_issuer_s_records_before_this_run(config, monkeypatch, dry_run):
    _run(config, monkeypatch, TEN)
    config.f1_prune_max_fraction = 0.09
    rep = _run(config, monkeypatch, [*TEN[:-1], _summary(2023, "new")], dry_run=dry_run)
    (err,) = [e for e in rep.errors if e["source"] == "f1_prune"]
    assert "refusing to prune 1 of 10 F1 record(s)" in err["error"]


def test_a_refused_issuer_is_listed_with_its_stale_count(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    rep = _run(config, monkeypatch, TEN[2:])
    assert rep.prune_refused_by_issuer == {CIK: 2}
    assert rep.prune_summary["refused_by_issuer"] == {CIK: 2}
    assert rep.prune_summary["by_issuer"] == {}


@pytest.mark.parametrize("payload", [
    {"message": "Not Found"},
    {"cik": 789019, "facts": {"us-gaap": {}}},
    {"cik": 320193, "facts": {}},
    {"cik": "not a cik", "facts": {"us-gaap": {}}},
])
def test_a_hollow_companyfacts_with_no_summary_is_not_a_clean_read(config, monkeypatch, payload):
    _run(config, monkeypatch, TEN)
    config.f1_prune_max_fraction = 1.0
    rep = _run(config, monkeypatch, [], facts=payload)
    assert rep.prune_skipped == 1 and rep.pruned == 0 and len(_f1_ids(config)) == 10


def test_a_companyfacts_without_cik_but_with_facts_is_a_clean_read(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    config.f1_prune_max_fraction = 1.0
    rep = _run(config, monkeypatch, [], facts={"facts": {"us-gaap": {}}})
    assert rep.pruned == 10 and rep.prune_skipped == 0


def test_a_duplicated_cik_is_read_once(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    _Source.plan = {CIK: (FACTS, TEN[1:], [], {})}
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _Source)
    rep = fetch_financials([CIK, "320193", "CIK0000320193"], config=config, fetcher=object(),
                           dry_run=True)
    assert rep.issuers == 1 and rep.periods == 9 and rep.would_prune == 1
    assert rep.prune_by_issuer == {CIK: 1}


def test_a_shrink_guard_refusal_skips_the_prune(config, monkeypatch):
    from company_corpus.storage import ShrinkGuardError
    _run(config, monkeypatch, TEN)

    def refusing(self, cik, rows):
        raise ShrinkGuardError(f"{cik}: refusing to drop 2 of 10 record group(s)")

    monkeypatch.setattr(Storage, "write_financials_table", refusing)
    rep = _run(config, monkeypatch, TEN[1:])
    assert rep.prune_skipped == 1 and rep.pruned == 0 and len(_f1_ids(config)) == 10


def test_a_fresh_issuer_with_facts_and_no_summary_writes_nothing(config, monkeypatch):
    rep = _run(config, monkeypatch, [])
    assert rep.pruned == 0 and rep.prune_skipped == 0 and rep.prune_refused == 0
    assert not (config.raw_dir / CIK).exists()
    assert not config.manifest_file(CIK).exists()
    assert not (config.financials_dir / f"{CIK}.jsonl").exists()
    assert not config.f1_prune_log_path.exists()


def test_an_unclean_issuer_then_a_clean_one_in_one_run(config, monkeypatch):
    other = normalize_cik("789019")
    _run(config, monkeypatch, TEN)
    _run(config, monkeypatch, TEN, cik=other, facts=OTHER_FACTS)
    error = {"source": "edgar_xbrl", "context": CIK, "error": "boom"}
    _Source.plan = {CIK: (FACTS, TEN[1:], [error], {}), other: (OTHER_FACTS, TEN[1:], [], {})}
    monkeypatch.setattr("company_corpus.pipeline.EdgarXBRL", _Source)
    rep = fetch_financials([CIK, other], config=config, fetcher=object(), dry_run=False)
    assert rep.prune_skipped == 1 and rep.pruned == 1 and rep.prune_by_issuer == {other: 1}
    assert len(_f1_ids(config)) == 10 and len(_f1_ids(config, other)) == 9


# ---- B3: CLI ----

from company_corpus import cli  # noqa: E402


def _cli(monkeypatch, config, *argv) -> int:
    monkeypatch.setenv("COMPANY_DATA_DIR", str(config.data_dir))
    return cli.main(["--data-dir", str(config.data_dir), "--contact", "test@example.com",
                     *argv])


def _run_report(config) -> dict:
    return json.loads((config.data_dir / "runs.jsonl").read_text().splitlines()[-1])


def test_cli_no_prune_and_allow_shrink(config, monkeypatch):
    captured: dict = {}

    def fake(ciks, **kw):
        captured.update(kw)
        return SimpleNamespace(issuers=1, periods=0, errors=[], coverage=Counter(),
                               stats=SimpleNamespace(seen=0, added=0, updated=0, unchanged=0),
                               prune_summary={"pruned": 0, "would_prune": 0, "refused": 0,
                                              "skipped": 0, "files": 0, "table_groups": 0,
                                              "records_refused": 0,
                                              "by_issuer": {}, "refused_by_issuer": {}})

    monkeypatch.setattr("company_corpus.cli.fetch_financials", fake)
    _cli(monkeypatch, config, "xbrl", "--ciks", "320193", "--no-prune")
    assert captured["prune"] is False
    assert _run_report(config)["details"]["xbrl"]["prune"] is None
    _cli(monkeypatch, config, "xbrl", "--ciks", "320193")
    assert captured["prune"] is True and captured["config"].f1_prune_max_fraction == 0.10
    _cli(monkeypatch, config, "xbrl", "--ciks", "320193", "--allow-shrink")
    assert captured["prune"] is True and captured["config"].f1_prune_max_fraction == 1.0


def test_cli_help_names_the_prune_flags(capsys):
    with pytest.raises(SystemExit):
        cli.main(["xbrl", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--no-prune" in out and "f1_prune_max_fraction" in out


def test_cli_prints_and_reports_the_prune(config, monkeypatch, capsys):
    _run(config, monkeypatch, TEN)
    _Source.plan = {CIK: (FACTS, TEN[1:], [], {})}
    rc = _cli(monkeypatch, config, "xbrl", "--ciks", CIK)
    out = capsys.readouterr().out
    assert rc == 0
    assert ("prune: pruned=0 would-prune=1 refused=0 skipped=0 files=2 table-groups=1"
            in out)
    prune = _run_report(config)["details"]["xbrl"]["prune"]
    assert prune["by_issuer"] == {CIK: 1} and prune["refused_by_issuer"] == {}
    assert prune["would_prune"] == 1 and len(_f1_ids(config)) == 10


def test_cli_no_prune_prints_no_prune_line(config, monkeypatch, capsys):
    _run(config, monkeypatch, TEN)
    _Source.plan = {CIK: (FACTS, TEN[1:], [], {})}
    _cli(monkeypatch, config, "xbrl", "--ciks", CIK, "--write", "--no-prune")
    assert "prune:" not in capsys.readouterr().out
    assert len(_f1_ids(config)) == 10


@pytest.mark.parametrize("write", [False, True])
def test_a_prune_refusal_is_a_local_refusal_not_a_sec_failure(config, monkeypatch, capsys,
                                                              write):
    _run(config, monkeypatch, TEN)
    _Source.plan = {CIK: (FACTS, TEN[2:], [], {})}
    rc = _cli(monkeypatch, config, "xbrl", "--ciks", CIK, *(["--write"] if write else []))
    rep = _run_report(config)
    (sec,) = rep["sources"]
    assert sec["docs_failed"] == 0 and sec["fetch_errors"] == 0 and sec["docs_new"] == 8
    assert rep["local_refusals"]["count"] == 1
    assert "refusing to prune 2 of 10" in rep["local_refusals"]["samples"][0]
    assert rep["totals"]["docs_failed"] == 1
    assert rep["details"]["xbrl"]["prune"]["refused_by_issuer"] == {CIK: 2}
    # useful work was done (8 period summaries): the doctrine keeps the run ok, the
    # refusal is named in local_refusals
    assert rc == 0 and rep["outcome"] == "ok"
    assert "prune: pruned=0 would-prune=0 refused=1" in capsys.readouterr().out


@pytest.mark.parametrize("write", [False, True])
def test_a_prune_refusal_with_no_summary_degrades_the_run(config, monkeypatch, write):
    _run(config, monkeypatch, TEN)
    _Source.plan = {CIK: (FACTS, [], [], {})}
    rc = _cli(monkeypatch, config, "xbrl", "--ciks", CIK, *(["--write"] if write else []))
    rep = _run_report(config)
    assert rep["sources"][0]["docs_failed"] == 0 and rep["local_refusals"]["count"] == 1
    assert rc == 3 and rep["outcome"] == "degraded"


def test_a_sec_error_beside_a_prune_refusal_stays_a_sec_failure(config, monkeypatch):
    other = normalize_cik("789019")
    _run(config, monkeypatch, TEN)
    _Source.plan = {CIK: (FACTS, TEN[2:], [], {}),
                    other: (None, [], [{"source": "edgar_xbrl", "context": other,
                                        "error": "companyfacts 500"}], {})}
    _cli(monkeypatch, config, "xbrl", "--ciks", f"{CIK},{other}")
    rep = _run_report(config)
    (sec,) = rep["sources"]
    assert sec["docs_failed"] == 1 and sec["fetch_errors"] == 1
    assert rep["local_refusals"]["count"] == 1 and rep["totals"]["docs_failed"] == 2


# ---- follow-ups of the final review ----

def test_refused_records_are_listed_by_prune_f1(config):
    st = Storage(config)
    bad, good, kept = _rec(2018), _rec(2019), _rec(2023)
    _seed(st, bad, good, kept)
    bad.primary_path = f"raw/{CIK}/F1/companyfacts.json"
    res = st.prune_f1(CIK, [bad, good], [kept], dry_run=True)
    assert res.refused == [bad.doc_id] and [r.doc_id for r in res.records] == [good.doc_id]


def test_a_failed_delete_is_a_refused_record(config, monkeypatch):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(
        PermissionError("locked")))
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert res.refused == [gone.doc_id] and not res.records


def test_records_refused_reach_the_report_and_the_cli_line(config, monkeypatch, capsys):
    _run(config, monkeypatch, TEN)
    m = Storage(config).load_manifest(CIK)
    gone = next(r for r in m.values() if r.period_of_report == date(2014, 12, 31))
    gone.primary_path = f"raw/{CIK}/F1/companyfacts.json"
    Storage(config)._write_manifest(CIK, m.values())
    rep = _run(config, monkeypatch, TEN[1:])
    assert rep.records_refused == 1 and rep.prune_summary["records_refused"] == 1
    _Source.plan = {CIK: (FACTS, TEN[1:], [], {})}
    _cli(monkeypatch, config, "xbrl", "--ciks", CIK)
    assert "records-refused=1" in capsys.readouterr().out
    assert _run_report(config)["details"]["xbrl"]["prune"]["records_refused"] == 1


def _failing_log(monkeypatch, config):
    real = Path.open

    def opener(self, mode="r", *a, **k):
        if self == config.f1_prune_log_path and "a" in mode:
            raise OSError("disk full")
        return real(self, mode, *a, **k)

    monkeypatch.setattr(Path, "open", opener)


def test_a_failed_log_append_keeps_the_prune_and_names_the_lost_tombstones(config, monkeypatch):
    st = Storage(config)
    gone, kept = _rec(2019), _rec(2023)
    _seed(st, gone, kept)
    _failing_log(monkeypatch, config)
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert [r.doc_id for r in res.records] == [gone.doc_id] and len(res.files) == 2
    assert set(st.load_manifest(CIK)) == {kept.doc_id} and not res.refused
    (err,) = res.errors
    assert err["source"] == "f1_prune" and err["doc_ids"] == [gone.doc_id]
    assert "tombstone" in err["error"] and "disk full" in err["error"]


def test_a_failed_log_append_still_counts_the_pruned_records(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    _failing_log(monkeypatch, config)
    rep = _run(config, monkeypatch, TEN[1:])
    assert rep.pruned == 1 and rep.prune_by_issuer == {CIK: 1} and rep.records_refused == 0
    assert any(e["source"] == "f1_prune" and len(e.get("doc_ids", [])) == 1 for e in rep.errors)
    assert len(_f1_ids(config)) == 9


def test_empty_statements_skip_the_prune_like_missing_ones(config, monkeypatch):
    _run(config, monkeypatch, TEN)
    rep = _run(config, monkeypatch, TEN[1:], coverage={"statements_empty": 1})
    assert rep.prune_skipped == 1 and rep.pruned == 0 and len(_f1_ids(config)) == 10
    config.f1_prune_requires_statements = False
    rep = _run(config, monkeypatch, TEN[1:], coverage={"statements_empty": 1})
    assert rep.pruned == 1


def test_a_companyfacts_of_another_cik_is_not_a_clean_read_even_with_summaries(config,
                                                                               monkeypatch):
    _run(config, monkeypatch, TEN)
    rep = _run(config, monkeypatch, TEN[1:], facts={"cik": 789019, "facts": {"us-gaap": {}}})
    assert rep.prune_skipped == 1 and rep.pruned == 0 and len(_f1_ids(config)) == 10


def test_a_companyfacts_of_another_cik_writes_nothing_and_is_an_error(config, monkeypatch):
    """Another issuer's document must not be filed under this cik: no summary, no table row,
    no companyfacts.json, one error item."""
    rep = _run(config, monkeypatch, TEN, facts=OTHER_FACTS)
    assert rep.periods == 0 and rep.prune_skipped == 1
    assert [e["source"] for e in rep.errors] == ["edgar_xbrl"] and "789019" in rep.errors[0]["error"]
    assert not (config.raw_dir / CIK).exists() and not (config.financials_dir / f"{CIK}.jsonl").exists()
    assert _f1_ids(config) == set()


def test_a_legacy_table_row_without_frequency_is_dropped_with_its_record(config):
    st = Storage(config)
    gone, kept = _rec(2019, frequency=""), _rec(2023)
    _seed(st, gone, kept)
    path = config.financials_dir / f"{CIK}.jsonl"
    rows = _table(config)
    for r in rows:
        if r["period_end"] == "2019-12-31":
            r.pop("frequency")
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert st.prune_f1(CIK, [gone], [kept], dry_run=True).table_groups == 1
    res = st.prune_f1(CIK, [gone], [kept], dry_run=False)
    assert res.table_groups == 1 and {r["period_end"] for r in _table(config)} == {"2023-12-31"}
