import json

import company_corpus.cli as cli


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
