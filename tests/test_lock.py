"""Two overlapping writers must not corrupt one corpus (DI-I1, Rob-I9, Contract I-7)."""
from __future__ import annotations

import threading

import pytest

from company_corpus.config import Config
from company_corpus.lock import CorpusLocked, corpus_lock, lock_path
from company_corpus.models import FilingRecord
from company_corpus.storage import Storage, _atomic_write_text
from company_corpus.taxonomy import FormType


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
    with corpus_lock(cfg, purpose="discover"):
        with pytest.raises(CorpusLocked) as excinfo:
            with corpus_lock(cfg, purpose="download"):
                pass
    message = str(excinfo.value)
    assert "discover" in message and str(lock_path(cfg)) in message


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
