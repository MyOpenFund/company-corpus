"""The read-only self-check: it must SEE every convergence break, and write nothing.

Fixtures here are deliberately imperfect -- a half-written manifest line, a row
filed under the wrong issuer, an id minted by the pre-chantier scheme -- because
a corpus that is already broken is the only one this command is for.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pytest

import company_corpus.cli as cli
from company_corpus.config import Config
from company_corpus.eu.documents import Document
from company_corpus.models import FilingRecord
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType
from company_corpus.verify import Finding, scan as verify_scan, verify


# ---------------------------------------------------------------------------
# fixtures: a small but REAL corpus, built through the writers themselves
# ---------------------------------------------------------------------------
def _rec(**kw) -> FilingRecord:
    base = dict(
        cik="320193", form_type=FormType.A1, sec_form="10-K",
        accession="0000320193-24-000123", company="Apple Inc.",
        filing_date=date(2024, 11, 1),
        submission_url="https://example.invalid/0000320193-24-000123.txt",
        primary_doc_url="https://example.invalid/aapl-20240928.htm",
    )
    base.update(kw)
    return FilingRecord(**base)


class _Fetcher:
    def __init__(self, body: str):
        self.body = body

    def get_text(self, url: str, **_) -> str:
        return self.body


def _row(period_end="2023-12-31", entity_id="0000320193", **kw) -> dict:
    row = {"entity_id": entity_id, "source": "sec", "period_end": period_end,
           "frequency": "annual", "basis": None, "kind": "reported",
           "concept": "assets", "value": 1, "unit": "USD"}
    row.update(kw)
    return row


@pytest.fixture
def corpus(config, sample_submission) -> Config:
    """A downloaded SEC filing, its financials table, and one EU document."""
    st = Storage(config)
    rec = _rec()
    st.fetch_and_store(rec, _Fetcher(sample_submission))
    st.save_records([rec], dry_run=False)
    st.write_financials_table("320193", [_row()])

    doc = Document(native_id="AR-2023-42", lei="5493001KJTIIGC8Y1R12", country="FI",
                   doc_type="annual_report", period_end=date(2023, 12, 31),
                   published_ts="2024-03-01T00:00:00Z", discovered_ts="2024-03-02",
                   language="fi", source="fin-oam")
    _write_eu_document(config, doc, "report.xhtml", b"<xhtml/>")
    return config


def _write_eu_document(config: Config, doc: Document, name: str, body: bytes,
                       **manifest_overrides) -> Path:
    """Write one EU raw file + its manifest exactly as ``eu.download`` does."""
    rel = Path("raw") / doc.lei / "ESEF-AR" / "2023" / doc.doc_id / name
    path = config.data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    manifest = {
        "doc_id": doc.doc_id, "native_id": doc.native_id, "lei": doc.lei,
        "country": doc.country, "doc_type": doc.doc_type,
        "period_end": doc.period_end.isoformat(), "published_ts": doc.published_ts,
        "discovered_ts": doc.discovered_ts, "language": doc.language,
        "source": doc.source,
        "files": [{"name": name, "url": "https://example.invalid/" + name,
                   "kind": "report", "sha256": hashlib.sha256(body).hexdigest(),
                   "path": str(rel)}],
        "native_meta": {},
    }
    manifest.update(manifest_overrides)
    mpath = config.data_dir / "manifest" / doc.lei / f"{doc.doc_id}.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=2))
    return mpath


def _manifest_rows(config: Config, cik="0000320193") -> list[dict]:
    path = config.manifest_dir / f"{cik}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_manifest_rows(config: Config, rows: list[dict], cik="0000320193") -> None:
    path = config.manifest_dir / f"{cik}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _kinds(findings: list[Finding]) -> list[str]:
    return [f.kind for f in findings]


# ---------------------------------------------------------------------------
# the clean case
# ---------------------------------------------------------------------------
def test_a_clean_corpus_reports_nothing(corpus):
    assert verify(corpus) == []


def test_a_clean_corpus_is_clean_under_hashing_too(corpus):
    assert verify(corpus, check_hashes=True) == []


def test_verify_writes_nothing_and_takes_no_lock(corpus):
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size)
              for p in corpus.data_dir.rglob("*") if p.is_file()}
    verify(corpus, check_hashes=True)
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size)
             for p in corpus.data_dir.rglob("*") if p.is_file()}
    assert after == before
    assert not (corpus.data_dir / ".corpus.lock").exists()


# ---------------------------------------------------------------------------
# the five checks of the brief
# ---------------------------------------------------------------------------
def test_a_dangling_primary_pointer_is_reported(corpus):
    rows = _manifest_rows(corpus)
    (corpus.data_dir / rows[0]["primary_path"]).unlink()

    findings = verify(corpus)
    missing = [f for f in findings if f.kind == "missing-artefact"]
    assert len(missing) == 1
    assert rows[0]["primary_path"] in missing[0].detail
    assert rows[0]["doc_id"] in missing[0].subject


def test_a_pointer_escaping_the_data_dir_is_reported(corpus):
    rows = _manifest_rows(corpus)
    rows[0]["text_path"] = "../../etc/passwd"
    _write_manifest_rows(corpus, rows)

    findings = verify(corpus)
    assert _kinds(findings) == ["missing-artefact"]
    assert "outside" in findings[0].detail


def test_a_stale_hash_is_found_only_when_hashing_is_asked_for(corpus):
    rows = _manifest_rows(corpus)
    (corpus.data_dir / rows[0]["local_path"]).write_text("truncated by something")

    assert verify(corpus) == []  # a metadata pass must not re-read 9 GB
    findings = verify(corpus, check_hashes=True)
    assert _kinds(findings) == ["hash-mismatch"]
    assert rows[0]["sha256"][:12] in findings[0].detail


def test_two_rows_sharing_a_doc_id_are_reported(corpus):
    rows = _manifest_rows(corpus)
    _write_manifest_rows(corpus, rows + [dict(rows[0], title="a second copy")])

    findings = verify(corpus)
    assert _kinds(findings) == ["duplicate-doc-id"]
    assert findings[0].subject.endswith(rows[0]["doc_id"])
    assert "2" in findings[0].detail


def test_a_table_row_filed_under_another_entity_is_reported(corpus):
    path = corpus.financials_dir / "0000320193.jsonl"
    path.write_text(json.dumps(_row()) + "\n"
                    + json.dumps(_row(entity_id="0000789019")) + "\n")

    findings = verify(corpus)
    assert _kinds(findings) == ["foreign-row"]
    assert "0000789019" in findings[0].detail


def test_a_raw_file_no_row_points_at_is_an_orphan(corpus):
    orphan = corpus.raw_dir / "0000320193" / "A1" / "2024" / "leftover.primary.htm"
    orphan.write_text("<html/>")

    findings = verify(corpus)
    assert _kinds(findings) == ["orphan-artefact"]
    assert findings[0].subject == "raw/0000320193/A1/2024/leftover.primary.htm"


def test_companyfacts_is_not_an_orphan(corpus):
    Storage(corpus).store_companyfacts("320193", {"cik": 320193, "facts": {}})
    assert verify(corpus) == []


# ---------------------------------------------------------------------------
# what the deployed corpus actually looks like (spec section 5)
# ---------------------------------------------------------------------------
def test_raw_bytes_with_no_manifest_at_all_are_all_orphans(config, sample_submission):
    """The measured migration case: 9 GB of raw bytes, no data/manifest/."""
    for name in ("a.submission.txt", "a.txt", "b.primary.html"):
        p = config.raw_dir / "0000320193" / "A1" / "2024" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("bytes")

    findings = verify(config)
    assert _kinds(findings) == ["orphan-artefact"] * 3
    assert not config.manifest_dir.exists()  # the check did not create it


def test_a_corpus_with_no_index_says_so_in_one_sentence(config):
    """N thousand orphan findings need the one note that explains all of them."""
    p = config.raw_dir / "0000320193" / "A1" / "2024" / "a.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("bytes")

    result = verify_scan(config)
    assert any("nothing indexes this corpus" in n for n in result.notes)
    assert any("no financials/ownership tables" in n for n in result.notes)


def test_a_healthy_corpus_gets_no_notes(corpus):
    assert verify_scan(corpus).notes == []


def test_cli_reports_the_notes(config, capsys):
    p = config.raw_dir / "0000320193" / "A1" / "2024" / "a.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("bytes")

    rc = cli.main(["--data-dir", str(config.data_dir), "verify", "--json"])
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert any("nothing indexes" in n for n in payload["notes"])
    assert payload["scanned"]["raw_files"] == 1


def test_an_old_scheme_f1_doc_id_is_reported_as_stale(corpus):
    """Task 3 added the period to family F's identity; a stored row minted before
    that carries an id the current rule cannot reproduce."""
    rec = _rec(form_type=FormType.F1, sec_form="10-K",
               period_of_report=date(2023, 9, 30), frequency="annual")
    row = rec.to_row()
    old_basis = f"{rec.cik}|{rec.form_type.code}|{rec.accession}"
    row["doc_id"] = hashlib.sha1(old_basis.encode()).hexdigest()[:16]
    row["local_path"] = row["primary_path"] = row["text_path"] = None
    row["sha256"] = None
    _write_manifest_rows(corpus, _manifest_rows(corpus) + [row])

    findings = verify(corpus)
    assert _kinds(findings) == ["stale-doc-id"]
    assert rec.doc_id in findings[0].detail


def test_an_f1_summary_left_by_the_old_scheme_is_an_orphan(corpus):
    stale = corpus.raw_dir / "0000320193" / "F1" / "2024" / "deadbeefdeadbeef.primary.html"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("<html/>")

    findings = verify(corpus)
    assert _kinds(findings) == ["orphan-artefact"]


def test_a_record_with_an_artefact_but_no_hash_is_incomplete(corpus):
    rows = _manifest_rows(corpus)
    rows[0]["sha256"] = None
    _write_manifest_rows(corpus, rows)

    findings = verify(corpus)
    assert _kinds(findings) == ["incomplete-record"]


# ---------------------------------------------------------------------------
# identifiers and unreadable bytes
# ---------------------------------------------------------------------------
def test_an_invalid_cik_in_a_row_is_reported_not_raised(corpus):
    rows = _manifest_rows(corpus)
    rows.append(dict(rows[0], cik="0000000000", doc_id="1111111111111111"))
    _write_manifest_rows(corpus, rows)

    findings = verify(corpus)
    assert _kinds(findings) == ["invalid-identifier"]
    assert "0000000000" in findings[0].detail


def test_a_row_filed_in_another_issuers_manifest_is_reported(corpus):
    rows = _manifest_rows(corpus)
    _write_manifest_rows(corpus, rows, cik="0000789019")

    findings = verify(corpus)
    assert "foreign-row" in _kinds(findings)


def test_a_manifest_named_by_a_non_cik_is_reported(corpus):
    (corpus.manifest_dir / "not-a-cik.jsonl").write_text("")

    findings = verify(corpus)
    assert _kinds(findings) == ["invalid-identifier"]
    assert "not-a-cik" in findings[0].subject


def test_a_table_named_by_a_malformed_lei_is_reported(corpus):
    corpus.financials_eu_dir.mkdir(parents=True, exist_ok=True)
    (corpus.financials_eu_dir / "not-a-lei.jsonl").write_text(
        json.dumps(_row(entity_id="not-a-lei")) + "\n")

    findings = verify(corpus)
    assert _kinds(findings) == ["invalid-identifier"]


def test_a_corrupt_manifest_line_is_reported_not_a_crash(corpus):
    path = corpus.manifest_dir / "0000320193.jsonl"
    path.write_text(path.read_text() + '{"cik": "0000320193", "form_ty\n')

    findings = verify(corpus)
    assert _kinds(findings) == ["unreadable-row"]
    assert findings[0].subject.endswith(":2")


def test_a_corrupt_table_line_is_reported_not_a_crash(corpus):
    path = corpus.financials_dir / "0000320193.jsonl"
    path.write_text(path.read_text() + "not json at all\n")

    findings = verify(corpus)
    assert _kinds(findings) == ["unreadable-row"]


def test_a_manifest_row_that_is_not_an_object_is_reported(corpus):
    path = corpus.manifest_dir / "0000320193.jsonl"
    path.write_text(path.read_text() + "[1, 2, 3]\n")

    findings = verify(corpus)
    assert _kinds(findings) == ["unreadable-row"]


# ---------------------------------------------------------------------------
# the EU pillar
# ---------------------------------------------------------------------------
def test_an_eu_manifest_without_a_native_id_cannot_be_checked(corpus):
    doc = Document(native_id="AR-2022-7", lei="5493001KJTIIGC8Y1R12", country="FI",
                   doc_type="annual_report", period_end=date(2023, 12, 31),
                   published_ts=None, discovered_ts="2024-03-02", language="fi",
                   source="fin-oam")
    mpath = _write_eu_document(corpus, doc, "old.xhtml", b"<xhtml/>")
    manifest = json.loads(mpath.read_text())
    del manifest["native_id"]  # the pre-Task-8 spelling
    mpath.write_text(json.dumps(manifest))

    findings = verify(corpus)
    assert _kinds(findings) == ["stale-doc-id"]
    assert "native_id" in findings[0].detail


def test_an_eu_doc_id_the_identity_rule_does_not_reproduce_is_stale(corpus):
    doc = Document(native_id="AR-2022-7", lei="5493001KJTIIGC8Y1R12", country="FI",
                   doc_type="annual_report", period_end=date(2023, 12, 31),
                   published_ts=None, discovered_ts="2024-03-02", language="fi",
                   source="fin-oam")
    mpath = _write_eu_document(corpus, doc, "old.xhtml", b"<xhtml/>")
    manifest = json.loads(mpath.read_text())
    manifest["native_id"] = "a different handle entirely"
    mpath.write_text(json.dumps(manifest))

    findings = verify(corpus)
    assert _kinds(findings) == ["stale-doc-id"]


def test_one_eu_doc_id_under_two_leis_is_a_duplicate(corpus):
    src = next((corpus.data_dir / "manifest").glob("*/*.json"))
    other = corpus.data_dir / "manifest" / "549300OTHERLEI00X999" / src.name
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(src.read_text())

    findings = verify(corpus)
    assert "duplicate-doc-id" in _kinds(findings)


def test_an_eu_file_pointer_with_no_bytes_is_reported(corpus):
    mpath = next((corpus.data_dir / "manifest").glob("*/*.json"))
    manifest = json.loads(mpath.read_text())
    (corpus.data_dir / manifest["files"][0]["path"]).unlink()

    findings = verify(corpus)
    assert _kinds(findings) == ["missing-artefact"]


def test_an_eu_file_hash_mismatch_needs_the_hash_flag(corpus):
    mpath = next((corpus.data_dir / "manifest").glob("*/*.json"))
    manifest = json.loads(mpath.read_text())
    (corpus.data_dir / manifest["files"][0]["path"]).write_bytes(b"other bytes")

    assert verify(corpus) == []
    assert _kinds(verify(corpus, check_hashes=True)) == ["hash-mismatch"]


def test_a_corrupt_eu_manifest_is_reported_not_a_crash(corpus):
    mpath = next((corpus.data_dir / "manifest").glob("*/*.json"))
    mpath.write_text("{not json")

    findings = verify(corpus)
    assert "unreadable-row" in _kinds(findings)


# ---------------------------------------------------------------------------
# ordering, filtering, and the CLI contract
# ---------------------------------------------------------------------------
def test_findings_come_back_in_a_stable_order(corpus):
    rows = _manifest_rows(corpus)
    (corpus.data_dir / rows[0]["primary_path"]).unlink()
    _write_manifest_rows(corpus, rows + [dict(rows[0], title="dupe")])
    (corpus.raw_dir / "0000320193" / "A1" / "2024" / "z-orphan.txt").write_text("x")
    (corpus.manifest_dir / "not-a-cik.jsonl").write_text("")

    first = verify(corpus)
    assert len(first) > 3
    assert first == sorted(first, key=lambda f: (f.kind, f.subject, f.detail))
    assert first == verify(corpus)


def test_ciks_narrows_the_check_to_those_issuers(corpus):
    (corpus.raw_dir / "0000789019" / "A1" / "2024").mkdir(parents=True)
    (corpus.raw_dir / "0000789019" / "A1" / "2024" / "orphan.txt").write_text("x")

    assert verify(corpus, ciks=["320193"]) == []
    assert _kinds(verify(corpus, ciks=["789019"])) == ["orphan-artefact"]


def test_a_pointer_outside_the_narrowed_subtree_is_not_called_missing(corpus):
    """A partial listing can prove presence, never absence."""
    other = corpus.raw_dir / "0000789019" / "A1" / "2024" / "shared.pdf"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("%PDF")
    rows = _manifest_rows(corpus)
    rows[0]["pdf_path"] = "raw/0000789019/A1/2024/shared.pdf"
    _write_manifest_rows(corpus, rows)

    assert verify(corpus, ciks=["320193"]) == []
    assert verify(corpus) == []


def test_cli_clean_corpus_exits_zero_with_an_empty_findings_list(corpus, capsys):
    rc = cli.main(["--data-dir", str(corpus.data_dir), "verify", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["findings"] == []


def test_cli_findings_exit_three_with_the_findings_on_stdout(corpus, capsys):
    rows = _manifest_rows(corpus)
    (corpus.data_dir / rows[0]["text_path"]).unlink()

    rc = cli.main(["--data-dir", str(corpus.data_dir), "verify", "--json"])
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert [f["kind"] for f in payload["findings"]] == ["missing-artefact"]
    assert payload["counts"]["missing-artefact"] == 1


def test_cli_text_output_of_a_clean_corpus_is_just_the_summary(corpus, capsys):
    rc = cli.main(["--data-dir", str(corpus.data_dir), "verify"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "findings: 0" in out
    assert "read-only" in out


def test_cli_a_bad_cik_flag_is_a_usage_error_not_a_verdict(corpus, capsys):
    rc = cli.main(["--data-dir", str(corpus.data_dir), "verify", "--ciks", "nope"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_cli_missing_data_dir_is_fatal(tmp_path, capsys):
    rc = cli.main(["--data-dir", str(tmp_path / "nope"), "verify"])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_cli_verify_takes_no_lock_and_writes_no_run_report(corpus, monkeypatch):
    """Read-only by construction: not a REPORTING_CMD, so no runs.jsonl either."""
    monkeypatch.setenv("COMPANY_DATA_DIR", str(corpus.data_dir))
    assert "verify" not in cli.REPORTING_CMDS

    def _no_lock(*a, **kw):  # pragma: no cover - only fires on a regression
        raise AssertionError("verify must not take the corpus lock")

    monkeypatch.setattr(cli, "corpus_lock", _no_lock)
    assert cli.main(["--data-dir", str(corpus.data_dir), "verify"]) == 0
    assert not (corpus.data_dir / "runs.jsonl").exists()


def test_cli_hash_flag_reaches_the_check(corpus, capsys):
    rows = _manifest_rows(corpus)
    (corpus.data_dir / rows[0]["local_path"]).write_text("tampered")

    assert cli.main(["--data-dir", str(corpus.data_dir), "verify"]) == 0
    capsys.readouterr()
    assert cli.main(["--data-dir", str(corpus.data_dir), "verify", "--hash"]) == 3


def test_cli_text_output_caps_the_examples_it_prints(corpus, capsys):
    d = corpus.raw_dir / "0000320193" / "A1" / "2024"
    for i in range(15):
        (d / f"orphan-{i:02d}.txt").write_text("x")

    rc = cli.main(["--data-dir", str(corpus.data_dir), "verify"])
    assert rc == 3
    out = capsys.readouterr().out
    assert "orphan-artefact: 15" in out
    assert out.count("orphan-") <= 12  # the count line + capped examples
    assert "5 more" in out


def test_unreadable_manifest_file_is_a_finding_not_a_crash(corpus):
    path = corpus.manifest_dir / "0000320193.jsonl"
    os.chmod(path, 0o000)
    try:
        if os.access(path, os.R_OK):  # pragma: no cover - running as root
            pytest.skip("running as root: an unreadable file cannot be staged")
        findings = verify(corpus)
    finally:
        os.chmod(path, 0o644)
    assert _kinds(findings) == ["unreadable-file"]
