"""Two overlapping writers must not corrupt one corpus (DI-I1, Rob-I9, Contract I-7)."""
from __future__ import annotations

import errno
import os
import stat
import threading
import time

import pytest

from company_corpus.config import Config
from company_corpus.lock import CorpusLocked, corpus_lock, lock_path
from company_corpus.models import FilingRecord
from company_corpus.storage import Storage, _atomic_write_text
from company_corpus.taxonomy import FormType
from company_corpus.universe import Issuer, Universe


def test_temp_name_is_unique_per_call(config, monkeypatch):
    seen: list[str] = []
    real_replace = __import__("os").replace

    def spy(src, dst):
        seen.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr("company_corpus.storage.os.replace", spy)
    path = config.data_dir / "x.jsonl"
    _atomic_write_text(path, "a\n")
    _atomic_write_text(path, "b\n")
    assert len(set(seen)) == 2
    assert path.read_text() == "b\n"


def test_a_failed_write_leaves_no_temp_file_behind(config, monkeypatch):
    """mkstemp creates the file itself, so a failure mid-write must clean up."""
    path = config.data_dir / "y.jsonl"
    _atomic_write_text(path, "first\n")
    monkeypatch.setattr("company_corpus.storage.os.replace",
                        lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        _atomic_write_text(path, "second\n")
    assert path.read_text() == "first\n"
    assert sorted(p.name for p in config.data_dir.iterdir()) == ["y.jsonl"]


def test_an_atomic_write_stays_readable_by_the_other_accounts(config, monkeypatch):
    """mkstemp hardcodes 0o600; a corpus only its writer can read is a regression.

    The RAG ingester and the NAS share consumers run as other accounts, so every
    manifest/table/extract must land with the mode a plain ``open()`` would have
    given it. ``storage._UMASK`` is an import-time snapshot (``os.umask`` has no
    getter), so pin it and the live umask to the standard 022.
    """
    monkeypatch.setattr("company_corpus.storage._UMASK", 0o022)
    previous = os.umask(0o022)
    try:
        path = config.data_dir / "perm.jsonl"
        _atomic_write_text(path, "row\n")
        mode = stat.S_IMODE(path.stat().st_mode)
    finally:
        os.umask(previous)
    assert mode & 0o044, f"{oct(mode)} is owner-only: other accounts cannot read it"
    assert mode == 0o644


def test_a_universe_save_is_atomic(config, monkeypatch):
    """`build-universe --write` must not truncate a committed list on failure."""
    uni = Universe(config)
    uni.save("curated", [Issuer(cik="0000320193", ticker="AAPL")])
    monkeypatch.setattr("company_corpus.storage.os.replace",
                        lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        uni.save("curated", [Issuer(cik="0000789019", ticker="MSFT")])
    assert "AAPL" in uni.path("curated").read_text(encoding="utf-8")


def test_concurrent_manifest_writers_lose_no_rows(config):
    def worker(tag: str, n: int):
        st = Storage(config)
        for i in range(n):
            with corpus_lock(Config(data_dir=config.data_dir, lock_wait_seconds=30.0),
                             purpose="test"):
                st.save_records([FilingRecord(
                    cik="320193", form_type=FormType.A1, sec_form="10-K",
                    accession=f"{tag}-{i}")], dry_run=False)

    threads = [threading.Thread(target=worker, args=(t, 40)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(Storage(config).load_manifest("320193")) == 80


def test_second_writer_fails_fast_and_names_the_holder(config):
    cfg = Config(data_dir=config.data_dir)
    started = time.monotonic()
    with corpus_lock(cfg, purpose="discover"):
        with pytest.raises(CorpusLocked) as excinfo:
            with corpus_lock(cfg, purpose="download"):
                pass
        elapsed = time.monotonic() - started
    message = str(excinfo.value)
    assert "discover" in message and str(lock_path(cfg)) in message
    # lock_wait_seconds defaults to 0.0: "fails fast" must mean no polling at all.
    assert elapsed < 0.5, f"the refusal took {elapsed:.2f}s: it waited"


def test_an_unlockable_filesystem_is_not_reported_as_contention(config, monkeypatch):
    """ENOTSUP/ENOLCK (SMB, some NFS mounts) means "cannot lock", not "held".

    Treating it as contention made every run on such a mount wait out
    lock_wait_seconds and then blame a phantom holder, hiding a data directory
    on which the single-writer guarantee does not exist at all.
    """
    def refuse(fd, operation):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr("company_corpus.lock.fcntl.flock", refuse)
    cfg = Config(data_dir=config.data_dir, lock_wait_seconds=1.0)
    started = time.monotonic()
    with pytest.raises(OSError) as excinfo:
        with corpus_lock(cfg, purpose="discover"):
            pass
    elapsed = time.monotonic() - started
    assert not isinstance(excinfo.value, CorpusLocked)
    assert excinfo.value.errno == errno.ENOTSUP
    message = str(excinfo.value)
    assert str(lock_path(cfg)) in message and "flock" in message
    assert elapsed < 0.5, f"waited {elapsed:.2f}s for a lock that can never be taken"


def test_a_bounded_wait_gives_up_and_still_names_the_holder(config):
    """lock_wait_seconds > 0 waits, but never forever."""
    cfg = Config(data_dir=config.data_dir, lock_wait_seconds=0.2)
    with corpus_lock(cfg, purpose="discover"):
        with pytest.raises(CorpusLocked) as excinfo:
            with corpus_lock(cfg, purpose="download"):
                pass
    assert "discover" in str(excinfo.value)


def test_an_unreadable_lock_file_still_yields_a_message(config):
    """The holder record is advisory: garbage in it must not mask the refusal."""
    cfg = Config(data_dir=config.data_dir)
    with corpus_lock(cfg, purpose="discover"):
        lock_path(cfg).write_text("not json at all", encoding="utf-8")
        with pytest.raises(CorpusLocked) as excinfo:
            with corpus_lock(cfg, purpose="download"):
                pass
    assert "holder unknown" in str(excinfo.value)


def test_lock_is_released_after_the_block(config):
    cfg = Config(data_dir=config.data_dir)
    with corpus_lock(cfg, purpose="a"):
        pass
    with corpus_lock(cfg, purpose="b"):
        pass


def test_lock_is_released_when_the_block_raises(config):
    cfg = Config(data_dir=config.data_dir)
    with pytest.raises(ValueError):
        with corpus_lock(cfg, purpose="a"):
            raise ValueError("boom")
    with corpus_lock(cfg, purpose="b"):
        pass


def test_eacces_is_not_contention_either(config, monkeypatch):
    """``flock`` never returns EACCES; ``lockf`` does. Waiting it out is wrong.

    EACCES sat in the contended set as a "harmless superset". It is not
    harmless: if anything ever hands this call an EACCES -- a future switch to
    ``lockf``, a filesystem that maps a permission refusal onto it -- the run
    would poll for ``lock_wait_seconds`` and then blame a holder that does not
    exist, instead of saying the corpus cannot be locked.
    """
    def refuse(fd, operation):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("company_corpus.lock.fcntl.flock", refuse)
    cfg = Config(data_dir=config.data_dir, lock_wait_seconds=1.0)
    started = time.monotonic()
    with pytest.raises(OSError) as excinfo:
        with corpus_lock(cfg, purpose="discover"):
            pass
    assert not isinstance(excinfo.value, CorpusLocked)
    assert excinfo.value.errno == errno.EACCES
    assert "cannot take the corpus lock" in str(excinfo.value)
    assert time.monotonic() - started < 0.5, "it waited for a lock it can never take"
