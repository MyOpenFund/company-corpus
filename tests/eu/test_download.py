from datetime import date
from pathlib import Path
import json
import os
import stat

from company_corpus.config import Config
from company_corpus.eu.documents import Document
from company_corpus.eu.download import download_document


class _DLFetcher:
    def download(self, url, dest, **_):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"PKG" + url.encode())
        return len(b"PKG" + url.encode())


class _FailingFetcher:
    """Fetcher whose download always raises, simulating a mid-stream failure."""
    def download(self, url, dest, **_):
        # Write partial content to dest (simulating corruption) then raise.
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"PARTIAL")
        raise OSError("connection reset")


def test_atomic_download_no_truncated_file_on_failure(tmp_path):
    """A failing download must leave NO file at dest — only a manifest error entry.
    The atomic .part + os.replace pattern ensures a truncated artifact can never
    be trusted by the idempotency check (dest.exists())."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    doc = Document(native_id="atomic-1", lei="L2", country="DE", doc_type="annual_report",
                   period_end=date(2023, 12, 31), published_ts="2024-03-01", discovered_ts="x",
                   language="de", source="filings.xbrl.org",
                   files=[{"name": "report.html", "url": "http://x/report.html", "kind": "report_url"}],
                   native_meta={})
    man = download_document(doc, fetcher=_FailingFetcher(), config=cfg)
    dest = cfg.raw_dir / "L2" / "ESEF-AR" / "2023" / doc.doc_id / "report.html"
    # dest must NOT exist — the failed .part file must have been cleaned up
    assert not dest.exists(), "truncated file must not survive a download failure"
    # The .part file must also be gone
    part = dest.with_name(dest.name + ".part")
    assert not part.exists(), ".part temp file must be cleaned up on failure"
    # The manifest must record the error so the failure is visible
    assert len(man["files"]) == 1 and "error" in man["files"][0]


def test_downloaded_files_stay_readable_by_the_other_accounts(tmp_path, monkeypatch):
    """The staging file is a mkstemp (0o600) inode the download only truncates.

    Whatever mode it carries is the mode the raw byte lands with, so it must be
    the one a plain open() would have produced -- otherwise the RAG ingester and
    the NAS share consumers cannot read a single downloaded document.
    """
    monkeypatch.setattr("company_corpus.storage._UMASK", 0o022)
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    doc = Document(native_id="perm-1", lei="L3", country="DE", doc_type="annual_report",
                   period_end=date(2023, 12, 31), published_ts="2024-03-01", discovered_ts="x",
                   language="de", source="filings.xbrl.org",
                   files=[{"name": "a.zip", "url": "http://x/a.zip", "kind": "package_url"},
                          {"name": "inline.txt", "content": "bytes", "kind": "report"}],
                   native_meta={})
    previous = os.umask(0o022)
    try:
        download_document(doc, fetcher=_DLFetcher(), config=cfg)
    finally:
        os.umask(previous)
    base = cfg.raw_dir / "L3" / "ESEF-AR" / "2023" / doc.doc_id
    for name in ("a.zip", "inline.txt"):
        mode = stat.S_IMODE((base / name).stat().st_mode)
        assert mode & 0o044, f"{name} is {oct(mode)}: other accounts cannot read it"


def test_download_writes_all_files_and_manifest(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    doc = Document(native_id="fxo-1", lei="L1", country="DE", doc_type="annual_report",
                   period_end=date(2023, 12, 31), published_ts="2024-03-01", discovered_ts="x",
                   language="de", source="filings.xbrl.org",
                   files=[{"name": "a.zip", "url": "http://x/a.zip", "kind": "package_url"},
                          {"name": "r.html", "url": "http://x/r.html", "kind": "report_url"}],
                   native_meta={})
    man = download_document(doc, fetcher=_DLFetcher(), config=cfg)
    base = cfg.raw_dir / "L1" / "ESEF-AR" / "2023" / doc.doc_id
    assert (base / "a.zip").exists() and (base / "r.html").exists()
    assert len(man["files"]) == 2 and all(f["sha256"] for f in man["files"])
    mpath = cfg.data_dir / "manifest" / "L1" / f"{doc.doc_id}.json"
    assert mpath.exists() and json.loads(mpath.read_text())["source"] == "filings.xbrl.org"


class _NoNetFetcher:
    """Fetcher whose .download must NEVER be called (inline-content path)."""
    def download(self, url, dest, **_):
        raise AssertionError("download() must not be called when content is inline")


def test_inline_content_is_written_without_fetching(tmp_path):
    """A file carrying inline `content` (e.g. Bundesanzeiger session-bound capture) is
    written directly; the network is never touched and `content` never leaks to the manifest."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    html = "<html><body>Dividendenbekanntmachung SAP SE</body></html>"
    doc = Document(native_id="de-1", lei="L9", country="DE", doc_type="inside_information",
                   period_end=date(2023, 6, 1), published_ts="2023-06-01", discovered_ts="x",
                   language="de", source="oam-de",
                   files=[{"name": "publication.html", "kind": "html",
                           "url": "https://www.bundesanzeiger.de/pub/de/suchen2?2-1.-ephemeral",
                           "content": html}],
                   native_meta={})
    man = download_document(doc, fetcher=_NoNetFetcher(), config=cfg)
    dest = cfg.raw_dir / "L9" / "MAR" / "2023" / doc.doc_id / "publication.html"
    assert dest.exists() and dest.read_text() == html
    f = man["files"][0]
    assert f["sha256"] and "content" not in f and f["kind"] == "html"
    assert f["url"].endswith("ephemeral"), "ephemeral url kept for provenance"


def test_index_only_file_recorded_without_download(tmp_path):
    """A file with neither content nor url (e.g. a DE capture that failed at discovery)
    is recorded in the manifest without any download attempt — no stale-link re-fetch."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    doc = Document(native_id="de-fail-1", lei="L7", country="DE", doc_type="inside_information",
                   period_end=date(2023, 5, 1), published_ts="2023-05-01", discovered_ts="x",
                   language="de", source="oam-de",
                   files=[{"name": "de-fail-1.html", "kind": "html", "capture_failed": True}],
                   native_meta={"detail_url": "https://www.bundesanzeiger.de/pub/de/suchen2?9-1.-ephemeral"})
    man = download_document(doc, fetcher=_NoNetFetcher(), config=cfg)
    f = man["files"][0]
    assert f.get("capture_failed") is True
    assert "sha256" not in f and "path" not in f, "index-only file is not downloaded"
    # No artifact written.
    assert not (cfg.raw_dir / "L7" / "MAR" / "2023" / doc.doc_id / "de-fail-1.html").exists()


# ---------------------------------------------------------------------------
# Task 7 — a hostile name from an OAM must cost a pretty filename, not the
# corpus (Rob-C7 / DI-M4).
# ---------------------------------------------------------------------------

def _doc(files, *, lei="5493001KJTIIGC8Y1R12", native_id="hostile-1"):
    return Document(native_id=native_id, lei=lei, country="DE", doc_type="annual_report",
                    period_end=date(2023, 12, 31), published_ts="2024-03-01",
                    discovered_ts="x", language="de", source="oam-de",
                    files=files, native_meta={})


def test_traversing_filename_stays_inside_the_document_directory(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    doc = _doc([{"name": "../../../pwn.bin", "url": "http://x/a", "kind": "package_url"}])
    man = download_document(doc, fetcher=_DLFetcher(), config=cfg)
    base = cfg.raw_dir / "5493001KJTIIGC8Y1R12" / "ESEF-AR" / "2023" / doc.doc_id
    f = man["files"][0]
    assert "error" not in f, "a hostile name must not cost us the document"
    assert f["name"].endswith(".bin") and "/" not in f["name"]
    assert (base / f["name"]).exists()
    # Nothing anywhere above the document directory.
    assert not (tmp_path / "pwn.bin").exists()
    assert not (cfg.raw_dir / "pwn.bin").exists()
    assert not (cfg.data_dir / "pwn.bin").exists()
    # The recorded path is relative to data_dir and points at the real file.
    assert not Path(f["path"]).is_absolute()
    assert (cfg.data_dir / f["path"]).exists()


def test_absolute_filename_cannot_write_outside_the_corpus(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    outside = tmp_path / "outside.bin"
    doc = _doc([{"name": str(outside), "url": "http://x/a", "kind": "package_url"}])
    man = download_document(doc, fetcher=_DLFetcher(), config=cfg)
    assert not outside.exists()
    assert (cfg.data_dir / man["files"][0]["path"]).exists()


def test_a_hostile_name_maps_to_the_same_file_on_a_re_run(tmp_path):
    """The fallback is a hash of the URL, so a second run is idempotent rather
    than downloading a second copy under a new name."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    files = [{"name": "../../../pwn.bin", "url": "http://x/a", "kind": "package_url"}]
    doc = _doc(files)
    first = download_document(doc, fetcher=_DLFetcher(), config=cfg)
    second = download_document(_doc(files), fetcher=_DLFetcher(), config=cfg)
    assert first["files"][0]["name"] == second["files"][0]["name"]
    # Same native id on both runs -> the same computed doc_id -> one directory.
    assert first["doc_id"] == second["doc_id"] == doc.doc_id
    base = cfg.raw_dir / "5493001KJTIIGC8Y1R12" / "ESEF-AR" / "2023" / doc.doc_id
    assert len(list(base.iterdir())) == 1


def test_a_hostile_lei_or_native_id_stays_under_the_raw_directory(tmp_path):
    """The LEI is still third-party text and still needs sanitising. The doc_id
    no longer can be hostile at all — Task 8 made it a computed hash — so a
    traversing native_id is neutralised before it is ever a path component."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    doc = _doc([{"name": "r.zip", "url": "http://x/a", "kind": "package_url"}],
               lei="../../..", native_id="../../pwn")
    assert "/" not in doc.doc_id and ".." not in doc.doc_id
    man = download_document(doc, fetcher=_DLFetcher(), config=cfg)
    written = [p for p in cfg.raw_dir.rglob("r.zip")]
    assert len(written) == 1
    assert cfg.raw_dir in written[0].parents
    assert not Path(man["files"][0]["path"]).is_absolute()
    # The manifest body and the manifest's own path agree, so acquire's
    # _discard_download can still find it: both are the sanitised components.
    written_manifests = list(cfg.data_dir.glob("manifest/*/*.json"))
    assert len(written_manifests) == 1
    assert (cfg.data_dir / "manifest" / man["lei"] / f"{man['doc_id']}.json"
            == written_manifests[0])


def test_a_path_outside_data_dir_is_a_recorded_error_not_a_traceback(tmp_path, monkeypatch):
    """``relative_to`` lives inside the try: it used to raise an uncaught
    ValueError that aborted the whole acquire run instead of costing one file."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")

    def _boom(self, *a, **kw):
        raise ValueError("is not in the subpath of")

    monkeypatch.setattr(Path, "relative_to", _boom)
    doc = _doc([{"name": "r.zip", "url": "http://x/a", "kind": "package_url"}])
    man = download_document(doc, fetcher=_DLFetcher(), config=cfg)
    f = man["files"][0]
    assert "is not in the subpath of" in f["error"]
    assert "path" not in f and "sha256" not in f


def test_manifest_carries_the_native_id_the_doc_id_was_computed_from(tmp_path):
    """``doc_id`` is a one-way hash: without the ``native_id`` beside it the
    manifest cannot say WHICH row of WHICH register the document came from, and
    an id can never be traced back to its source (or recomputed to check it)."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    doc = Document(native_id="ÖVERLÅTELSE/2024-17", lei="L9", country="SE",
                   doc_type="holding_notification", period_end=None,
                   published_ts="2024-03-01", discovered_ts="x", language="sv",
                   source="oam-se",
                   files=[{"name": "a.pdf", "url": "http://x/a.pdf", "kind": "document"}],
                   native_meta={})
    man = download_document(doc, fetcher=_DLFetcher(), config=cfg)
    assert man["native_id"] == "ÖVERLÅTELSE/2024-17"
    on_disk = json.loads(
        (cfg.data_dir / "manifest" / man["lei"] / f"{man['doc_id']}.json").read_text())
    assert on_disk["native_id"] == doc.native_id
    assert on_disk["source"] == doc.source and on_disk["country"] == doc.country
