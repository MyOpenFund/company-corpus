"""migrate-contract (chantier 4, spec §5.3): a corpus written before the vault contract is
rewritten in it -- rows in the contract, F1 moved to its index, primary documents hashed from
the files on disk, vocabulary.json written -- under a dry run first, idempotently, keyed by
doc_id, with local reads only.

The fixture corpus is built by the real writers, then put back in the pre-contract shape
(old row names, F1 rows in the manifest, no primary hash, no index, no vocabulary)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import company_corpus.cli as cli
from company_corpus.config import Config, normalize_cik
from company_corpus.lock import corpus_lock
from company_corpus.migrate import contract_names, migrate_contract
from company_corpus.models import FilingRecord
from company_corpus.ownership import find_ownership_doc, form345_text, parse_form345, \
    render_form345_html
from company_corpus.storage import Storage, primary_document, text_sha256
from company_corpus.vocabulary import vocabulary_text
from company_corpus.taxonomy import FormType
from company_corpus.verify import verify

from .test_ownership import FORM4_SUBMISSION

APPLE, MSFT, AMZN = normalize_cik("320193"), normalize_cik("789019"), normalize_cik("1018724")


class _Fetcher:
    def __init__(self, body: str):
        self.body = body

    def get_text(self, url: str, **_) -> str:
        return self.body


def _rec(cik, form_type, sec_form, accession, filing_date=date(2024, 11, 1), **kw):
    acc = accession.replace("-", "")
    return FilingRecord(
        cik=cik, form_type=form_type, sec_form=sec_form, accession=accession,
        title=f"Issuer {cik} {sec_form}", company=f"Issuer {cik}", filing_date=filing_date,
        primary_doc_url=kw.pop("primary_doc_url",
                               f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/"
                               "aapl-20240928.htm"),
        submission_url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{accession}.txt",
        provenance="edgar_submissions", **kw)


def _f1(cik, year):
    return FilingRecord(cik=cik, form_type=FormType.F1, sec_form="10-K/XBRL",
                        accession=f"{cik}-{year % 100:02d}-000001",
                        filing_date=date(year + 1, 2, 1), period_of_report=date(year, 12, 31),
                        frequency="annual", provenance="edgar_xbrl",
                        title=f"Issuer {cik} — FY{year} financial summary")


def _old_row(rec: FilingRecord) -> dict:
    """The row the pre-contract writer (company-corpus 06c619d) produced for ``rec``."""
    return {
        "cik": rec.cik, "form_type": rec.form_type.code, "sec_form": rec.sec_form,
        "accession": rec.accession, "title": rec.title, "company": rec.company,
        "company_current": rec.company_current, "ticker": rec.ticker,
        "entity_id": rec.entity_id,
        "filing_date": rec.filing_date.isoformat() if rec.filing_date else None,
        "period_of_report": rec.period_of_report.isoformat() if rec.period_of_report else None,
        "frequency": rec.frequency, "primary_doc_url": rec.primary_doc_url,
        "submission_url": rec.submission_url, "provenance": rec.provenance,
        "sha256": rec.submission_sha256 if rec.submission_path else rec.primary_sha256,
        "local_path": rec.submission_path, "primary_path": rec.primary_path,
        "text_path": rec.text_path, "pdf_path": rec.pdf_path, "language": rec.language,
        "alt_urls": rec.alt_urls, "family": rec.form_type.family, "doc_id": rec.doc_id,
        "year": rec.year,
    }


def _write_old_manifest(config: Config, cik: str, recs) -> None:
    path = config.manifest_file(cik)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(_old_row(r), ensure_ascii=False) + "\n" for r in recs),
                    encoding="utf-8")


@pytest.fixture
def old_corpus(config, sample_submission) -> Config:
    """Three issuers in the pre-contract shape.

    Apple: a 10-K downloaded and rendered; a Form 4 downloaded and summarized (the summary
    superseded the decomposed XML); an 8-K discovered, never downloaded; an undated S-1; a
    10-Q whose submission yields no primary document (no primary, no text); two F1 summaries.
    Microsoft: a 10-Q downloaded, text only; one F1 summary. Amazon: F1 summaries only.
    """
    st = Storage(config)
    apple_10k = _rec(APPLE, FormType.A1, "10-K", "0000320193-24-000123")
    st.fetch_and_store(apple_10k, _Fetcher(sample_submission))
    pdf = config.data_dir / st.raw_dir_for(apple_10k) / f"{apple_10k.doc_id}.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    apple_10k.pdf_path = str(pdf.relative_to(config.data_dir))

    form4 = _rec(APPLE, FormType.E1, "4", "0001214156-24-000060", filing_date=date(2024, 5, 1),
                 primary_doc_url="https://www.sec.gov/Archives/edgar/data/320193/"
                                 "000121415624000060/form4.xml")
    st.fetch_and_store(form4, _Fetcher(FORM4_SUBMISSION))
    filing = parse_form345(find_ownership_doc(FORM4_SUBMISSION, "E1"))
    st.write_ownership_summary(form4, render_form345_html(filing), form345_text(filing))

    eight_k = _rec(APPLE, FormType.B1, "8-K", "0000320193-24-000070")
    undated = _rec(APPLE, FormType.D1, "S-1", "0000320193-99-000001", filing_date=None)
    no_primary = _rec(APPLE, FormType.A2, "10-Q", "0000320193-24-000081",
                      primary_doc_url="https://www.sec.gov/Archives/edgar/data/320193/"
                                      "000032019324000081/missing.htm")
    st.fetch_and_store(no_primary, _Fetcher("<SEC-DOCUMENT>\n<DOCUMENT>\n<TYPE>10-Q\n"
                                            "<FILENAME>missing.htm\n<TEXT>\n</TEXT>\n"
                                            "</DOCUMENT>\n</SEC-DOCUMENT>\n"))
    assert no_primary.submission_path and not no_primary.primary_path

    apple_f1 = [_f1(APPLE, 2022), _f1(APPLE, 2023)]
    msft_10q = _rec(MSFT, FormType.A2, "10-Q", "0000789019-24-000010")
    st.fetch_and_store(msft_10q, _Fetcher(sample_submission))
    msft_f1 = [_f1(MSFT, 2023)]
    amzn_f1 = [_f1(AMZN, 2022), _f1(AMZN, 2023)]
    for r in apple_f1 + msft_f1 + amzn_f1:
        st.write_financial_summary(r, f"<p>{r.doc_id}</p>", r.doc_id)

    # Back to the pre-contract shape: no primary hash for real filings (the old download did
    # not stamp one), F1 in the manifest, no F1 index, no vocabulary.
    for r in (apple_10k, form4, msft_10q, no_primary):
        r.primary_sha256 = None
    _write_old_manifest(config, APPLE, [apple_10k, form4, eight_k, undated, no_primary,
                                        *apple_f1])
    _write_old_manifest(config, MSFT, [msft_10q, *msft_f1])
    _write_old_manifest(config, AMZN, amzn_f1)
    assert not config.f1_index_dir.exists() and not config.vocabulary_path.exists()
    return config


def _snapshot(config: Config) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in sorted(config.data_dir.rglob("*")) if p.is_file()}


def _doc_ids(config: Config) -> set[str]:
    ids = set()
    for d in (config.manifest_dir, config.f1_index_dir):
        for path in d.glob("*.jsonl") if d.is_dir() else ():
            ids |= {json.loads(x)["doc_id"] for x in path.read_text().splitlines() if x.strip()}
    return ids


def _rows(config: Config, cik: str, *, f1: bool = False) -> list[dict]:
    path = config.f1_index_file(cik) if f1 else config.manifest_file(cik)
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# ---- dry run ----

def test_the_dry_run_writes_nothing_and_counts_what_it_would_do(old_corpus):
    before = _snapshot(old_corpus)
    rep = migrate_contract(old_corpus, dry_run=True)
    assert _snapshot(old_corpus) == before
    assert rep.counts == {
        "issuers": 3, "issuers_refused": 0, "rows": 11, "rows_rewritten": 6, "unchanged": 0,
        "f1_moved": 5, "f1_already_indexed": 0, "f1_dropped_tombstoned": 0,
        "primary_hashed": 3, "primary_unhashable": 0,
        "no_primary": 3, "no_rag_artifact": 3, "provenance_unknown": 0,
    }
    assert rep.errors == []


def test_the_pre_migration_corpus_shows_its_debt_in_verify(old_corpus):
    findings = verify(old_corpus)
    assert [f.kind for f in findings] == ["pre-contract-row"] * 11
    assert all("migrate-contract" in f.detail for f in findings)


# ---- write ----

def test_write_rewrites_every_row_in_the_contract_and_moves_f1(old_corpus):
    ids_before = _doc_ids(old_corpus)
    rep = migrate_contract(old_corpus, dry_run=False)
    assert rep.errors == []
    assert (rep.counts["rows_rewritten"], rep.counts["f1_moved"],
            rep.counts["primary_hashed"]) == (6, 5, 3)
    assert _doc_ids(old_corpus) == ids_before, "keyed by doc_id: no identity changes"

    apple = _rows(old_corpus, APPLE)
    assert [r["doc_type"] for r in apple] == ["D1", "E1", "B1", "A2", "A1"]  # date, accession
    assert all(r["corpus"] == "company" and r["source_code"] == "sec" for r in apple)
    assert {r["doc_type"] for r in _rows(old_corpus, APPLE, f1=True)} == {"F1"}
    assert _rows(old_corpus, AMZN) == [], "an F1-only issuer keeps an empty manifest"
    assert len(_rows(old_corpus, AMZN, f1=True)) == 2
    assert json.loads(old_corpus.vocabulary_path.read_text())["corpus"] == "company"


def test_the_primary_hashes_are_the_filed_documents(old_corpus):
    migrate_contract(old_corpus, dry_run=False)
    by_type = {r["doc_type"]: r for r in _rows(old_corpus, APPLE)}
    tenk = by_type["A1"]
    assert tenk["sha256"] == hashlib.sha256(
        (old_corpus.data_dir / tenk["primary_path"]).read_bytes()).hexdigest()
    assert (tenk["local_path"], tenk["mime_type"]) == (tenk["pdf_path"], "application/pdf")
    # The Form 4: the file at primary_path is the ownership summary; sha256 is the XML filed.
    form4 = by_type["E1"]
    assert form4["primary_path"].endswith(".primary.html")
    assert form4["sha256"] != hashlib.sha256(
        (old_corpus.data_dir / form4["primary_path"]).read_bytes()).hexdigest()
    filed = primary_document(FilingRecord.from_row(form4), FORM4_SUBMISSION)
    assert form4["sha256"] == text_sha256(filed.text)
    # Inventory rows: no artefact, no hash.
    for code in ("B1", "D1", "A2"):
        assert (by_type[code]["local_path"], by_type[code]["sha256"]) == (None, None), code
    assert by_type["D1"]["date"] is None and by_type["D1"]["date_precision"] is None


def test_after_the_write_verify_with_hashes_finds_nothing(old_corpus):
    migrate_contract(old_corpus, dry_run=False)
    assert verify(old_corpus, check_hashes=True) == []


def test_a_rerun_changes_nothing(old_corpus):
    migrate_contract(old_corpus, dry_run=False)
    before = _snapshot(old_corpus)
    rep = migrate_contract(old_corpus, dry_run=False)
    assert (rep.counts["rows_rewritten"], rep.counts["f1_moved"], rep.counts["primary_hashed"],
            rep.counts["unchanged"]) == (0, 0, 0, 6)
    assert _snapshot(old_corpus) == before
    assert migrate_contract(old_corpus, dry_run=True).counts["rows_rewritten"] == 0


def test_an_f1_row_already_in_the_index_keeps_the_index_version(old_corpus):
    # An xbrl run between the code update and the migration wrote the index already.
    st = Storage(old_corpus)
    newer = _f1(APPLE, 2023)
    newer.title = "written by the new code"
    st.write_financial_summary(newer, "<p>new</p>", "new")
    st.save_f1([newer], dry_run=False)
    rep = migrate_contract(old_corpus, dry_run=False)
    assert (rep.counts["f1_moved"], rep.counts["f1_already_indexed"]) == (5, 1)
    titles = {r["doc_id"]: r["title"] for r in _rows(old_corpus, APPLE, f1=True)}
    assert titles[newer.doc_id] == "written by the new code" and len(titles) == 2


def test_ciks_narrow_the_migration(old_corpus):
    rep = migrate_contract(old_corpus, ciks=[MSFT], dry_run=False)
    assert rep.counts["issuers"] == 1 and rep.counts["f1_moved"] == 1
    assert "form_type" in _rows(old_corpus, APPLE)[0], "Apple is untouched"
    assert old_corpus.vocabulary_path.exists()


# ---- adversarial: the issuer is refused, its files are untouched ----

def _refused(config, cik):
    before = config.manifest_file(cik).read_bytes()
    rep = migrate_contract(config, dry_run=False)
    assert config.manifest_file(cik).read_bytes() == before
    assert not config.f1_index_file(cik).exists()
    assert rep.counts["issuers_refused"] == 1
    return rep


def test_a_stored_doc_id_the_identity_rule_does_not_reproduce_refuses_the_issuer(old_corpus):
    rows = _rows(old_corpus, MSFT)
    rows[0]["doc_id"] = "0123456789abcdef"
    old_corpus.manifest_file(MSFT).write_text("".join(json.dumps(r) + "\n" for r in rows))
    rep = _refused(old_corpus, MSFT)
    assert "0123456789abcdef" in rep.errors[0]["error"]
    assert rep.counts["rows_rewritten"] == 5, "the other issuers migrate"


def test_an_unparseable_line_refuses_the_issuer(old_corpus):
    path = old_corpus.manifest_file(MSFT)
    path.write_text(path.read_text() + '{"cik": "0000789019", "form_ty\n')
    rep = _refused(old_corpus, MSFT)
    assert ":3" in rep.errors[0]["error"]


def test_a_duplicated_doc_id_refuses_the_issuer(old_corpus):
    path = old_corpus.manifest_file(MSFT)
    path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
    rep = _refused(old_corpus, MSFT)
    assert "duplicate" in rep.errors[0]["error"]


def test_a_manifest_named_by_no_cik_is_refused(old_corpus):
    (old_corpus.manifest_dir / "notacik.jsonl").write_text("")
    rep = migrate_contract(old_corpus, dry_run=True)
    assert rep.counts["issuers_refused"] == 1 and "notacik" in rep.errors[0]["error"]


def test_a_primary_with_no_bytes_left_is_counted_unhashable(old_corpus):
    rows = _rows(old_corpus, MSFT)
    (old_corpus.data_dir / rows[0]["local_path"]).unlink()      # the submission
    (old_corpus.data_dir / rows[0]["primary_path"]).unlink()
    rep = migrate_contract(old_corpus, dry_run=False)
    assert (rep.counts["primary_hashed"], rep.counts["primary_unhashable"]) == (2, 1)
    # Nothing a re-run can do about it: the dry run says so too, no endless "rewrite".
    again = migrate_contract(old_corpus, dry_run=True).counts
    assert (again["rows_rewritten"], again["primary_unhashable"]) == (0, 1)
    assert "missing-primary-hash" in [f.kind for f in verify(old_corpus)]


# ---- the vault's own checker over the migrated manifests ----

def _vault_check(config: Config, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "vault_contract.check", str(config.manifest_dir),
         "--corpus", "company", "--vocabulary", str(config.vocabulary_path), *extra],
        capture_output=True, text=True, check=False)


def test_the_vault_checker_accepts_the_migrated_manifests(old_corpus):
    migrate_contract(old_corpus, dry_run=False)
    result = _vault_check(old_corpus)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refusals: 0" in result.stdout
    final = _vault_check(old_corpus, "--no-transition", "--strict")
    assert final.returncode == 0, final.stdout + final.stderr
    assert "notices: 0" in final.stdout


def test_the_vault_checker_refuses_the_pre_migration_manifests(old_corpus):
    old_corpus.vocabulary_path.write_text(vocabulary_text(old_corpus))
    result = _vault_check(old_corpus)
    assert result.returncode == 1
    assert "missing_doc_type" in result.stdout


# ---- the CLI ----

def test_cli_dry_run_then_write_then_rerun(old_corpus, capsys, monkeypatch):
    monkeypatch.setenv("COMPANY_DATA_DIR", str(old_corpus.data_dir))
    argv = ["--data-dir", str(old_corpus.data_dir), "migrate-contract"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "rows_rewritten=6" in out and "f1_moved=5" in out
    assert not old_corpus.f1_index_dir.exists()
    assert cli.main(argv + ["--write"]) == 0
    assert "WROTE" in capsys.readouterr().out
    assert cli.main(argv + ["--write"]) == 0
    assert "rows_rewritten=0" in capsys.readouterr().out
    runs = [json.loads(x) for x in (old_corpus.data_dir / "runs.jsonl").read_text().splitlines()]
    assert [r["command"] for r in runs] == ["migrate-contract"] * 3
    assert runs[1]["details"]["migrate_contract"]["f1_moved"] == 5
    assert runs[2]["outcome"] == "ok"


def test_cli_write_takes_the_corpus_lock(old_corpus, capsys, monkeypatch):
    monkeypatch.setenv("COMPANY_DATA_DIR", str(old_corpus.data_dir))
    with corpus_lock(old_corpus, purpose="test"):
        rc = cli.main(["--data-dir", str(old_corpus.data_dir), "migrate-contract", "--write"])
    assert rc == 1 and "another company-corpus run" in capsys.readouterr().err
    assert not old_corpus.f1_index_dir.exists()


def test_cli_a_refused_issuer_alone_degrades_the_run(old_corpus, capsys, monkeypatch):
    monkeypatch.setenv("COMPANY_DATA_DIR", str(old_corpus.data_dir))
    for cik in (APPLE, MSFT, AMZN):
        old_corpus.manifest_file(cik).unlink()
    (old_corpus.manifest_dir / f"{APPLE}.jsonl").write_text("not json\n")
    rc = cli.main(["--data-dir", str(old_corpus.data_dir), "migrate-contract", "--write"])
    assert rc == 3


# ---- F1 rows pruned by the new code must not come back (tombstones) ----

def _prune_in_index(config, gone, keep):
    st = Storage(config)
    st.save_f1([gone, keep], dry_run=False)
    res = st.prune_f1(APPLE, [gone], [keep], dry_run=False, run_id="r")
    assert [r.doc_id for r in res.records] == [gone.doc_id]
    assert not (config.data_dir / gone.primary_path).exists()


def _apple_f1(config):
    recs = {r.doc_id: r for r in (FilingRecord.from_row(contract_names(x)) for x in _rows(config, APPLE))
            if r.form_type is FormType.F1}
    return sorted(recs.values(), key=lambda r: r.period_of_report)


def test_two_xbrl_writes_before_the_migration_do_not_resurrect_a_pruned_f1(old_corpus):
    gone, keep = _apple_f1(old_corpus)
    _prune_in_index(old_corpus, gone, keep)        # the second xbrl --write pruned 2022
    dry = migrate_contract(old_corpus, dry_run=True).counts
    assert dry["f1_dropped_tombstoned"] == 1
    rep = migrate_contract(old_corpus, dry_run=False)
    assert (rep.counts["f1_moved"], rep.counts["f1_dropped_tombstoned"]) == (5, 1)
    assert {r["doc_id"] for r in _rows(old_corpus, APPLE, f1=True)} == {keep.doc_id}
    assert verify(old_corpus) == []


def test_a_crash_mid_migration_then_a_prune_does_not_resurrect_it(old_corpus, monkeypatch):
    real = Storage._write_manifest

    def crash(self, cik, records):
        if cik == APPLE:
            raise OSError("disk full")
        return real(self, cik, records)

    monkeypatch.setattr(Storage, "_write_manifest", crash)
    with pytest.raises(OSError):
        migrate_contract(old_corpus, dry_run=False)
    monkeypatch.setattr(Storage, "_write_manifest", real)
    gone, keep = _apple_f1(old_corpus)               # F1 in both the manifest and the index
    assert {r["doc_id"] for r in _rows(old_corpus, APPLE, f1=True)} == {gone.doc_id, keep.doc_id}
    _prune_in_index(old_corpus, Storage(old_corpus).load_f1(APPLE)[gone.doc_id],
                    Storage(old_corpus).load_f1(APPLE)[keep.doc_id])
    rep = migrate_contract(old_corpus, dry_run=False)
    assert rep.counts["f1_dropped_tombstoned"] == 1
    assert {r["doc_id"] for r in _rows(old_corpus, APPLE, f1=True)} == {keep.doc_id}
    assert verify(old_corpus) == []


def test_a_tombstoned_f1_whose_files_are_still_there_is_moved(old_corpus):
    gone, _ = _apple_f1(old_corpus)
    old_corpus.f1_prune_log_path.write_text(json.dumps({"doc_id": gone.doc_id}) + "\n")
    rep = migrate_contract(old_corpus, dry_run=False)
    assert rep.counts["f1_dropped_tombstoned"] == 0
    assert gone.doc_id in {r["doc_id"] for r in _rows(old_corpus, APPLE, f1=True)}


def test_a_provenance_outside_the_vocabulary_is_reported(old_corpus, capsys, monkeypatch):
    # The vault would refuse the row (unknown_provenance): say so before the NAS copy.
    rows = _rows(old_corpus, MSFT)
    rows[0]["provenance"] = "manual"
    old_corpus.manifest_file(MSFT).write_text("".join(json.dumps(r) + "\n" for r in rows))
    rep = migrate_contract(old_corpus, dry_run=True)
    assert rep.counts["provenance_unknown"] == 1
    assert rep.unknown_provenances == {"manual": 1}
    monkeypatch.setenv("COMPANY_DATA_DIR", str(old_corpus.data_dir))
    cli.main(["--data-dir", str(old_corpus.data_dir), "migrate-contract"])
    assert "provenance outside the vocabulary: manual=1" in capsys.readouterr().err


def test_a_cik_without_a_manifest_is_reported(old_corpus, capsys, monkeypatch):
    rep = migrate_contract(old_corpus, ciks=[MSFT, "1234"], dry_run=True)
    assert rep.counts["issuers"] == 1 and rep.ciks_without_manifest == ["0000001234"]
    monkeypatch.setenv("COMPANY_DATA_DIR", str(old_corpus.data_dir))
    cli.main(["--data-dir", str(old_corpus.data_dir), "migrate-contract", "--ciks", "1234"])
    assert "no manifest for: 0000001234" in capsys.readouterr().err


# ---- the migration reads the pre-contract form on its own ----

FIXTURES = Path(__file__).parent / "fixtures" / "contract"


def _fixture_rows() -> list[dict]:
    return [json.loads(x) for x in
            (FIXTURES / "pre_contract_rows.jsonl").read_text(encoding="utf-8").splitlines()]


def test_contract_names_renames_a_pre_contract_row_with_a_submission():
    from company_corpus.migrate import contract_names
    old = _fixture_rows()[0]
    new = contract_names(old)
    assert (new["doc_type"], new["date"], new["document_url"]) == (
        old["form_type"], old["filing_date"], old["primary_doc_url"])
    assert (new["submission_path"], new["submission_sha256"]) == (old["local_path"], old["sha256"])
    assert new["sha256"] is None, "the old sha256 hashed the submission, not the primary"
    assert not {"form_type", "filing_date", "primary_doc_url", "local_path"} & set(new)
    rec = FilingRecord.from_row(new)
    assert rec.doc_id == old["doc_id"] and rec.submission_path == old["local_path"]
    assert old["form_type"] == "A1", "the input row is not modified"


def test_contract_names_keeps_the_primary_hash_of_a_row_without_submission():
    from company_corpus.migrate import contract_names
    old = _fixture_rows()[1]
    assert old["local_path"] is None and old["sha256"]
    rec = FilingRecord.from_row(contract_names(old))
    assert (rec.primary_sha256, rec.submission_sha256, rec.submission_path) == \
        (old["sha256"], None, None)


def test_contract_names_leaves_a_contract_row_alone():
    from company_corpus.migrate import contract_names
    row = _rec(APPLE, FormType.A1, "10-K", "0000320193-24-000123").to_row()
    assert contract_names(row) == row
