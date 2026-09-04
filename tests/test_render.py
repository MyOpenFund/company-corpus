from __future__ import annotations

import shutil
from datetime import date

import pytest

from company_corpus.models import FilingRecord
from company_corpus.pipeline import render_universe
from company_corpus.render import find_chrome, make_chrome_renderer
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType


def fake_renderer(src, dst):
    dst.write_bytes(b"%PDF-1.4 fake pdf")


def _record_with_primary(config, accession="0000320193-24-000123"):
    st = Storage(config)
    rec = FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K",
                       accession=accession, company="Apple Inc.",
                       filing_date=date(2024, 11, 1))
    dest = st.raw_dir_for(rec)
    dest.mkdir(parents=True, exist_ok=True)
    primary = dest / f"{rec.doc_id}.primary.htm"
    primary.write_text("<html><body>hi</body></html>", encoding="utf-8")
    rec.primary_path = str(primary.relative_to(config.data_dir))
    return st, rec


def test_render_record_writes_pdf(config):
    st, rec = _record_with_primary(config)
    res = st.render_record(rec, fake_renderer, dry_run=False)
    assert res.status == "rendered"
    assert rec.pdf_path and (config.data_dir / rec.pdf_path).exists()


def test_render_is_idempotent(config):
    st, rec = _record_with_primary(config)
    st.render_record(rec, fake_renderer, dry_run=False)
    assert st.render_record(rec, fake_renderer, dry_run=False).status == "skipped"


def test_render_dry_run_writes_nothing(config):
    st, rec = _record_with_primary(config)
    res = st.render_record(rec, fake_renderer, dry_run=True)
    assert res.status == "would-render"
    assert rec.pdf_path is None


def test_render_no_primary(config):
    st = Storage(config)
    rec = FilingRecord(cik="320193", form_type=FormType.A1, sec_form="10-K",
                       accession="x", filing_date=date(2024, 11, 1))
    assert st.render_record(rec, fake_renderer, dry_run=False).status == "no-primary"


def test_render_error_is_captured(config):
    st, rec = _record_with_primary(config)

    def boom(src, dst):
        raise RuntimeError("chrome failed")

    res = st.render_record(rec, boom, dry_run=False)
    assert res.status == "error" and "chrome failed" in res.error


def test_render_universe_updates_manifest(config):
    st, rec = _record_with_primary(config)
    st.save_records([rec], dry_run=False)
    rep = render_universe(["320193"], renderer=fake_renderer, dry_run=False,
                          config=config, storage=st)
    assert rep.rendered == 1
    assert all(r.pdf_path for r in st.load_manifest("320193").values())


def test_find_chrome_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "mychrome"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("COMPANY_CORPUS_CHROME", str(fake))
    assert find_chrome() == str(fake)


def test_make_chrome_renderer_missing_raises(monkeypatch):
    monkeypatch.delenv("COMPANY_CORPUS_CHROME", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        make_chrome_renderer()
