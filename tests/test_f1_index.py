"""F1 out of the manifest (chantier 4, spec §5.2): the vault reads manifest/, which holds
only vault documents (Config.vault_families); F1 records live in derived/f1/<cik>.jsonl."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from company_corpus.completeness import build_matrix
from company_corpus.config import Config, normalize_cik
from company_corpus.models import FilingRecord
from company_corpus.storage import ManifestNeedsMigration, Storage
from company_corpus.taxonomy import FormType
from company_corpus.verify import verify

CIK = normalize_cik("320193")


def _f1(year: int) -> FilingRecord:
    end = date(year, 12, 31)
    return FilingRecord(cik=CIK, form_type=FormType.F1, sec_form="10-K/XBRL",
                        accession=f"0000320193-{year % 100:02d}-000001",
                        filing_date=end + timedelta(days=50), period_of_report=end,
                        frequency="annual", provenance="edgar_xbrl",
                        title=f"Apple Inc. — FY{year} financial summary")


def _a1(n: int = 1) -> FilingRecord:
    return FilingRecord(cik=CIK, form_type=FormType.A1, sec_form="10-K",
                        accession=f"0000320193-24-{n:06d}", filing_date=date(2024, 11, 1),
                        title="Apple Inc. 10-K")


def _seed_f1(st: Storage, *recs: FilingRecord) -> None:
    for r in recs:
        st.write_financial_summary(r, f"<p>{r.doc_id}</p>", r.doc_id)
    st.save_f1(recs, dry_run=False)


def test_f1_records_are_written_to_the_f1_index_only(config):
    st = Storage(config)
    _seed_f1(st, _f1(2023))
    assert config.f1_index_file(CIK) == config.data_dir / "derived" / "f1" / f"{CIK}.jsonl"
    assert config.f1_index_file(CIK).exists()
    assert not config.manifest_file(CIK).exists()
    assert list(st.load_f1(CIK)) == [_f1(2023).doc_id]
    assert st.load_manifest(CIK) == {}
    assert not config.vocabulary_path.exists(), "the F1 index is not a vault manifest"


def test_an_f1_record_passed_to_the_manifest_is_refused_before_any_write(config):
    st = Storage(config)
    with pytest.raises(ValueError, match="F1"):
        st.save_records([_a1(), _f1(2023)], dry_run=False)
    assert not config.manifest_file(CIK).exists()


def test_a_family_outside_vault_families_is_refused(tmp_path):
    st = Storage(Config(data_dir=tmp_path, vault_families=("A",)))
    st.save_records([_a1()], dry_run=False)
    b1 = FilingRecord(cik=CIK, form_type=FormType.B1, sec_form="8-K", accession="0000320193-24-000009")
    with pytest.raises(ValueError, match="vault_families"):
        st.save_records([b1], dry_run=False)


def test_the_f1_index_takes_only_f1(config):
    with pytest.raises(ValueError, match="A1"):
        Storage(config).save_f1([_a1()], dry_run=False)


def test_the_f1_index_merges_like_a_manifest(config):
    st = Storage(config)
    _seed_f1(st, _f1(2023))
    fresh = _f1(2023)                          # a re-run: no pointers, no hash
    stats = st.save_f1([fresh, _f1(2022)], dry_run=False)
    assert (stats.added, stats.unchanged) == (1, 1)
    kept = st.load_f1(CIK)[fresh.doc_id]
    assert kept.primary_path and kept.primary_sha256, "sticky pointers carried forward"


# ---- a manifest written before chantier 4 (adversarial) ----

def _pre_migration_manifest(config) -> tuple[FilingRecord, FilingRecord]:
    """An A1 and an F1 row in one manifest, in the old row format."""
    a1, f1 = _a1(), _f1(2023)
    rows = []
    for rec in (a1, f1):
        row = {"cik": rec.cik, "form_type": rec.form_type.code, "sec_form": rec.sec_form,
               "accession": rec.accession, "title": rec.title,
               "filing_date": rec.filing_date.isoformat(),
               "period_of_report": rec.period_of_report.isoformat()
               if rec.period_of_report else None, "frequency": rec.frequency,
               "provenance": rec.provenance, "sha256": None, "local_path": None,
               "primary_path": None, "text_path": None, "pdf_path": None,
               "doc_id": rec.doc_id, "family": rec.form_type.family}
        rows.append(row)
    path = config.manifest_file(CIK)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return a1, f1


def test_reading_a_pre_migration_manifest_fails_loudly_naming_the_migration(config):
    _pre_migration_manifest(config)
    with pytest.raises(ManifestNeedsMigration, match="migrate-contract"):
        Storage(config).load_manifest(CIK)


def test_a_pre_contract_row_among_contract_rows_is_not_skipped(config):
    # A mixed manifest (a contract row, then one old row): skipping the old row would let the
    # next write drop it.
    st = Storage(config)
    st.save_records([_a1(1)], dry_run=False)
    path = config.manifest_file(CIK)
    old = {"cik": CIK, "form_type": "A1", "sec_form": "10-K", "accession": _a1(2).accession,
           "filing_date": "2024-11-01", "doc_id": _a1(2).doc_id}
    path.write_text(path.read_text() + json.dumps(old) + "\n")
    with pytest.raises(ManifestNeedsMigration, match="migrate-contract"):
        st.load_manifest(CIK)


def test_writing_a_pre_migration_manifest_fails_before_any_write(config):
    _pre_migration_manifest(config)
    before = config.manifest_file(CIK).read_text()
    st = Storage(config)
    for dry_run in (True, False):
        with pytest.raises(ManifestNeedsMigration, match="migrate-contract"):
            st.save_records([_a1(2)], dry_run=dry_run)
    assert config.manifest_file(CIK).read_text() == before


def test_an_f1_row_in_a_manifest_fails_loudly(config):
    # Contract-form F1 in manifest/ (never written by this code): an error, not a skip.
    st = Storage(config)
    st.save_records([_a1()], dry_run=False)
    path = config.manifest_file(CIK)
    path.write_text(path.read_text() + json.dumps(_f1(2023).to_row()) + "\n")
    with pytest.raises(RuntimeError, match="an F1 row"):
        st.load_manifest(CIK)
    with pytest.raises(RuntimeError, match="F1"):
        st.save_records([_a1(2)], dry_run=False)


def test_verify_reports_an_f1_row_in_a_manifest(config):
    st = Storage(config)
    st.save_records([_a1()], dry_run=False)
    f1 = _f1(2023)
    path = config.manifest_file(CIK)
    path.write_text(path.read_text() + json.dumps(f1.to_row()) + "\n")
    findings = verify(config)
    assert [f.kind for f in findings] == ["f1-in-manifest"]
    assert f1.doc_id in findings[0].subject and "derived/f1" in findings[0].detail


def test_a_non_f1_row_in_the_f1_index_fails_loudly(config):
    st = Storage(config)
    _seed_f1(st, _f1(2023))
    path = config.f1_index_file(CIK)
    path.write_text(path.read_text() + json.dumps(_a1().to_row()) + "\n")
    before = path.read_text()
    with pytest.raises(RuntimeError, match="A1"):
        st.save_f1([_f1(2022)], dry_run=False)
    assert path.read_text() == before


# ---- readers of F1 ----

def test_verify_covers_the_f1_index(config):
    st = Storage(config)
    rec = _f1(2023)
    _seed_f1(st, rec)
    assert verify(config, check_hashes=True) == []        # its files are referenced
    (config.data_dir / rec.text_path).unlink()
    findings = verify(config)
    assert [f.kind for f in findings] == ["missing-artefact"]
    assert "derived/f1" in findings[0].subject


def test_an_f1_index_row_of_another_family_is_reported(config):
    _seed_f1(Storage(config), _f1(2023))
    path = config.f1_index_file(CIK)
    path.write_text(path.read_text() + json.dumps(_a1().to_row()) + "\n")
    assert [f.kind for f in verify(config)] == ["not-f1-in-f1-index"]


def test_the_completeness_report_still_counts_f1(config):
    st = Storage(config)
    _seed_f1(st, _f1(2023))
    st.save_records([_a1()], dry_run=False)
    rows = build_matrix([CIK], [2024], (FormType.A1, FormType.F1), storage=st, config=config)
    assert {r["form_type"]: r["discovered"] for r in rows} == {"A1": 1, "F1": 1}


@pytest.mark.parametrize("cmd", ["download", "render-pdf", "ownership"])
@pytest.mark.parametrize("write", [False, True])
def test_a_command_reading_a_pre_migration_manifest_fails_naming_the_migration(
        config, cmd, write, capsys, monkeypatch):
    # No preflight scan: the failure comes from reading the manifest, before any network
    # work for that issuer, and nothing is written.
    import company_corpus.cli as cli
    _pre_migration_manifest(config)
    before = config.manifest_file(CIK).read_text()
    monkeypatch.setenv("COMPANY_DATA_DIR", str(config.data_dir))
    argv = ["--data-dir", str(config.data_dir), cmd, "--ciks", "320193"]
    assert cli.main(argv + (["--write"] if write else [])) == 1
    err = capsys.readouterr().err
    assert "migrate-contract" in err and CIK in err
    assert config.manifest_file(CIK).read_text() == before
    assert not config.f1_index_dir.exists()


def test_a_writing_run_does_not_scan_the_manifests_it_does_not_read(config, monkeypatch):
    # The per-run scan of every manifest is gone: an issuer outside the run is not read.
    import company_corpus.cli as cli
    _pre_migration_manifest(config)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(config.data_dir))
    assert not hasattr(Storage, "manifests_needing_migration")
    assert cli.main(["--data-dir", str(config.data_dir), "download", "--ciks", "789019",
                     "--write"]) == 0


def test_a_migrated_corpus_is_read_by_the_writing_commands(config, monkeypatch):
    import company_corpus.cli as cli
    from company_corpus.migrate import migrate_contract
    _pre_migration_manifest(config)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(config.data_dir))
    migrate_contract(config, dry_run=False)
    assert cli.main(["--data-dir", str(config.data_dir), "download", "--ciks", "320193",
                     "--limit", "0", "--write"]) == 0


def test_render_pdf_says_f1_is_no_longer_rendered(config, capsys, monkeypatch):
    import company_corpus.cli as cli
    monkeypatch.setenv("COMPANY_DATA_DIR", str(config.data_dir))
    cli.main(["--data-dir", str(config.data_dir), "render-pdf", "--ciks", "320193",
              "--forms", "F1"])
    assert "F1 is no longer rendered" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["pre-contract", "f1-in-manifest"])
def test_report_on_a_manifest_it_cannot_read_is_an_error_not_a_traceback(
        config, bad, capsys, monkeypatch):
    # `report` is not a reporting command: main() itself turns the reader's refusal into
    # `error: ...` and exit code 1.
    import company_corpus.cli as cli
    if bad == "pre-contract":
        _pre_migration_manifest(config)
    else:
        st = Storage(config)
        st.save_records([_a1()], dry_run=False)
        path = config.manifest_file(CIK)
        path.write_text(path.read_text() + json.dumps(_f1(2023).to_row()) + "\n")
    before = config.manifest_file(CIK).read_bytes()
    monkeypatch.setenv("COMPANY_DATA_DIR", str(config.data_dir))
    rc = cli.main(["--data-dir", str(config.data_dir), "report", "--ciks", "320193",
                   "--years", "2024"])
    err = capsys.readouterr().err
    assert rc == 1
    assert err.startswith("error: ") and CIK in err and "Traceback" not in err
    assert ("migrate-contract" if bad == "pre-contract" else "F1") in err
    assert config.manifest_file(CIK).read_bytes() == before


MASTER_IDX = """CIK|Company Name|Form Type|Date Filed|Filename
320193|Apple Inc.|10-K|2024-11-01|edgar/data/320193/0000320193-24-000123.txt
"""


@pytest.mark.parametrize("cmd", ["discover", "discover-index"])
@pytest.mark.parametrize("write", [False, True])
def test_discovery_on_a_pre_migration_manifest_fails_cleanly_and_writes_nothing(
        config, cmd, write, capsys, monkeypatch):
    # Discovery merges into the manifest, so it reads it first: a pre-contract manifest
    # stops the run (exit 1, the migration named), and the manifest is left byte for byte.
    import company_corpus.cli as cli
    import company_corpus.pipeline as pipeline
    from .conftest import APPLE_SUBMISSIONS, FakeFetcher

    def fake(cfg):
        return FakeFetcher({"CIK0000320193.json": APPLE_SUBMISSIONS,
                            "master.idx": MASTER_IDX}, config=cfg)

    monkeypatch.setattr(pipeline, "Fetcher", fake)
    monkeypatch.setattr(cli, "Fetcher", fake)
    _pre_migration_manifest(config)
    before = config.manifest_file(CIK).read_bytes()
    monkeypatch.setenv("COMPANY_DATA_DIR", str(config.data_dir))
    argv = ["--data-dir", str(config.data_dir), cmd, "--ciks", "320193", "--years", "2024"]
    rc = cli.main(argv + (["--write"] if write else []))
    err = capsys.readouterr().err
    assert rc == 1
    assert "error: " in err and "migrate-contract" in err and CIK in err
    assert "Traceback" not in err
    assert config.manifest_file(CIK).read_bytes() == before
    assert not config.f1_index_dir.exists()
