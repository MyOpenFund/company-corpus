import json

import company_corpus.cli as cli
from company_corpus.config import Config
from company_corpus.lock import corpus_lock, lock_path


def _read_report(data_dir):
    lines = (data_dir / "runs.jsonl").read_text().strip().split("\n")
    return json.loads(lines[-1])


def test_discover_clean_run_exits_zero_and_reports(tmp_path, monkeypatch):
    def fake_cmd_discover(args):
        args.report.source("sec").record_saved_counts({"saved": 2})
        return 0

    monkeypatch.setattr(cli, "_cmd_discover", fake_cmd_discover)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["discover", "--universe", "x"])
    assert rc == 0
    rep = _read_report(tmp_path)
    assert rep["outcome"] == "ok"
    assert rep["tool"] == "company-corpus"
    assert rep["command"] == "discover"
    assert rep["exit_code"] == 0


def test_discover_truncated_source_exits_three(tmp_path, monkeypatch):
    def fake_cmd_discover(args):
        args.report.source("sec").record_fetch_error("listing unreachable", truncated=True)
        return 0

    monkeypatch.setattr(cli, "_cmd_discover", fake_cmd_discover)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["discover", "--universe", "x"])
    assert rc == 3
    assert _read_report(tmp_path)["outcome"] == "degraded"


def test_crash_writes_failed_report_and_exits_one(tmp_path, monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(cli, "_cmd_discover", boom)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["discover", "--universe", "x"])
    assert rc == 1
    rep = _read_report(tmp_path)
    assert rep["outcome"] == "failed"
    assert "adapter exploded" in rep["fatal"]
    err = capsys.readouterr().err
    assert "error:" in err


def test_list_forms_stays_reportless_and_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["list-forms"])
    assert rc == 0
    assert not (tmp_path / "runs.jsonl").exists()


def test_data_dir_flag_is_honoured_when_env_unset(tmp_path, monkeypatch):
    def fake_cmd_discover(args):
        args.report.source("sec").record_saved_counts({"saved": 1})
        return 0

    monkeypatch.setattr(cli, "_cmd_discover", fake_cmd_discover)
    monkeypatch.delenv("COMPANY_DATA_DIR", raising=False)
    rc = cli.main(["--data-dir", str(tmp_path), "discover", "--universe", "x"])
    assert rc == 0
    rep = _read_report(tmp_path)
    assert rep["outcome"] == "ok"


def test_render_pdf_nonzero_return_is_folded_into_failed(tmp_path, monkeypatch, capsys):
    def fake_cmd_render_pdf(args):
        print("render-pdf: Chrome not installed", file=__import__("sys").stderr)
        return 1

    monkeypatch.setattr(cli, "_cmd_render_pdf", fake_cmd_render_pdf)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["render-pdf", "--ciks", "0000320193"])
    assert rc == 1
    rep = _read_report(tmp_path)
    assert rep["outcome"] == "failed"
    assert "exit code 1" in rep["fatal"]
    err = capsys.readouterr().err
    assert "error: render-pdf returned exit code 1" in err


def test_render_pdf_none_return_is_treated_as_ok(tmp_path, monkeypatch):
    def fake_cmd_render_pdf(args):
        args.report.source("sec").record_saved_counts({"saved": 1})
        return None

    monkeypatch.setattr(cli, "_cmd_render_pdf", fake_cmd_render_pdf)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["render-pdf", "--ciks", "0000320193"])
    assert rc == 0
    rep = _read_report(tmp_path)
    assert rep["outcome"] == "ok"


def test_zero_return_is_treated_as_ok(tmp_path, monkeypatch):
    def fake_cmd_render_pdf(args):
        args.report.source("sec").record_saved_counts({"saved": 1})
        return 0

    monkeypatch.setattr(cli, "_cmd_render_pdf", fake_cmd_render_pdf)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["render-pdf", "--ciks", "0000320193"])
    assert rc == 0
    rep = _read_report(tmp_path)
    assert rep["outcome"] == "ok"


def test_unwritable_report_path_exits_nonzero_without_traceback(tmp_path, monkeypatch, capsys):
    def fake_cmd_discover(args):
        args.report.source("sec").record_saved_counts({"saved": 1})
        return 0

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    bad_data_dir = blocker / "data"

    monkeypatch.setattr(cli, "_cmd_discover", fake_cmd_discover)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(bad_data_dir))
    rc = cli.main(["discover", "--universe", "x"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error: could not write run report" in err
    assert "Traceback" not in err


def test_a_writing_run_refuses_to_start_while_the_lock_is_held(tmp_path, monkeypatch, capsys):
    """A second writer must fail fast with a message naming the holder, not corrupt."""
    def fake_cmd_discover(args):
        raise AssertionError("the command must not run while another writer holds the lock")

    monkeypatch.setattr(cli, "_cmd_discover", fake_cmd_discover)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    cfg = Config(data_dir=tmp_path)
    with corpus_lock(cfg, purpose="download"):
        rc = cli.main(["--data-dir", str(tmp_path), "discover", "--ciks", "320193", "--write"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "download" in err and str(lock_path(cfg)) in err
    rep = _read_report(tmp_path)
    assert rep["outcome"] == "failed"
    assert "download" in rep["fatal"]


def test_a_universe_write_refuses_to_start_while_the_lock_is_held(tmp_path, monkeypatch, capsys):
    """`build-universe --write` rewrites data/universe/*.jsonl: it is a writer too."""
    def fake_cmd_build_universe(args):
        raise AssertionError("build-universe must not run while another writer holds the lock")

    monkeypatch.setattr(cli, "_cmd_build_universe", fake_cmd_build_universe)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    cfg = Config(data_dir=tmp_path)
    with corpus_lock(cfg, purpose="discover"):
        rc = cli.main(["--data-dir", str(tmp_path), "build-universe",
                       "--tickers", "AAPL", "--write"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "discover" in err and str(lock_path(cfg)) in err


def test_a_read_only_run_ignores_a_held_lock(tmp_path, monkeypatch):
    """A dry run writes nothing, so it must never block on a running crawl."""
    def fake_cmd_discover(args):
        args.report.source("sec").record_saved_counts({"saved": 0})
        return 0

    monkeypatch.setattr(cli, "_cmd_discover", fake_cmd_discover)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    with corpus_lock(Config(data_dir=tmp_path), purpose="download"):
        rc = cli.main(["--data-dir", str(tmp_path), "discover", "--ciks", "320193"])
    assert rc == 0


def test_a_read_only_run_does_not_create_the_lock_file(tmp_path, monkeypatch):
    def fake_cmd_discover(args):
        args.report.source("sec").record_saved_counts({"saved": 0})
        return 0

    monkeypatch.setattr(cli, "_cmd_discover", fake_cmd_discover)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    data_dir = tmp_path / "corpus"
    assert cli.main(["--data-dir", str(data_dir), "discover", "--ciks", "320193"]) == 0
    assert not lock_path(Config(data_dir=data_dir)).exists()
