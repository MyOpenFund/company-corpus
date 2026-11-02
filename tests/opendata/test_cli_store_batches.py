import json

import pytest

pytest.importorskip("myopendata")

import company_corpus.cli as cli  # noqa: E402
from tests.opendata.test_batches import A, B, _issuer  # noqa: E402


def _report(data_dir):
    return json.loads((data_dir / "runs.jsonl").read_text().strip().split("\n")[-1])


def _main(config, monkeypatch, *args):
    monkeypatch.setenv("COMPANY_DATA_DIR", str(config.data_dir))
    return cli.main(["--data-dir", str(config.data_dir), "store-batches", *args])


def test_write_run_exits_zero_and_reports_details(config, tmp_path, monkeypatch, capsys):
    _issuer(config, A), _issuer(config, B)
    rc = _main(config, monkeypatch, "--ciks", f"{A},{B}", "--landing", str(tmp_path / "l"),
               "--write")
    assert rc == 0
    rep = _report(config.data_dir)
    assert rep["command"] == "store-batches" and rep["outcome"] == "ok"
    assert rep["details"]["store_batches"]["totals"]["ok"] == 2
    assert "store-batches [WROTE]" in capsys.readouterr().out


def test_an_issuer_without_companyfacts_mid_run_exits_three(config, tmp_path, monkeypatch):
    _issuer(config, B)
    rc = _main(config, monkeypatch, "--ciks", f"{A},{B}", "--landing", str(tmp_path / "l"),
               "--write")
    assert rc == 3
    rep = _report(config.data_dir)
    assert rep["outcome"] == "degraded"
    assert rep["details"]["store_batches"]["issuers"][B]["status"] == "ok"
    assert list((tmp_path / "l" / "fundamentals_v2").iterdir())


def test_dry_run_takes_no_lock_and_writes_no_landing(config, tmp_path, monkeypatch):
    from company_corpus.lock import lock_path

    _issuer(config, A)
    rc = _main(config, monkeypatch, "--ciks", A, "--landing", str(tmp_path / "l"))
    assert rc == 0 and not (tmp_path / "l").exists() and not lock_path(config).exists()


def test_unwritable_landing_is_fatal(config, tmp_path, monkeypatch):
    _issuer(config, A)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    rc = _main(config, monkeypatch, "--ciks", A, "--landing", str(blocker / "l"), "--write")
    assert rc == 1


def test_refresh_with_offline_is_a_usage_error(config, tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as e:
        _main(config, monkeypatch, "--ciks", A, "--landing", str(tmp_path / "l"),
              "--refresh-submissions", "--offline")
    assert e.value.code == 2


def test_missing_myopendata_is_fatal(config, tmp_path, monkeypatch):
    import company_corpus.opendata.batches as batches

    def unavailable():
        raise batches.StoreUnavailable("store-batches needs my-open-data")

    monkeypatch.setattr(batches, "_myopendata", unavailable)
    _issuer(config, A)
    assert _main(config, monkeypatch, "--ciks", A, "--landing", str(tmp_path / "l")) == 1
