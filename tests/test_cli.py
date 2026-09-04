from __future__ import annotations

import json
import types
from datetime import date

import pytest

from company_corpus.cli import _parse_years, main
from company_corpus.config import Config
from company_corpus.universe import Issuer, Universe


@pytest.fixture(autouse=True)
def _stub_name_fetch(monkeypatch):
    """Default-stub the name-tier SEC download so CLI tests stay network-free.
    Tests that exercise name resolution override this with their own stub."""
    monkeypatch.setattr("company_corpus.cli.fetch_cik_lookup", lambda fetcher, path: "")


def _stats():
    return types.SimpleNamespace(seen=0, added=0, updated=0, unchanged=0)


def _disc_report():
    return types.SimpleNamespace(issuers=1, rounds=1, stats=_stats(), errors=[])


def _dl_report():
    return types.SimpleNamespace(downloaded=0, skipped=0, errors=0, bytes=0,
                                 empty=0, error_items=[])


def test_list_forms_default_scope(capsys):
    rc = main(["list-forms"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "A1" in out and "10-K" in out
    assert "E1" in out  # --forms all shows opt-in families too


def test_list_forms_family_filter(capsys):
    rc = main(["list-forms", "--forms", "A"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "A1" in out
    assert "B1" not in out


def test_config_command(capsys):
    rc = main(["config"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "user_agent" in out
    assert "SEC max 10" in out


def test_parse_years_is_an_inclusive_range():
    assert _parse_years("2015-2018") == [2015, 2016, 2017, 2018]
    assert _parse_years("2024") == [2024]


def test_data_dir_flag_overrides_config(capsys, tmp_path):
    rc = main(["--data-dir", str(tmp_path), "config"])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(tmp_path) in out  # the override is reflected, not ./data


def test_insecure_flag_disables_tls_verification(capsys):
    rc = main(["--insecure", "config"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "verify_tls        : False" in out


def test_tls_verification_on_by_default_in_config(capsys):
    rc = main(["config"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "verify_tls        : True" in out


def test_data_dir_threaded_into_pipeline(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setattr("company_corpus.cli.discover_universe",
                        lambda ciks, **kw: _disc_report())

    def fake_download(ciks, **kw):
        captured.update(kw)
        return _dl_report()

    monkeypatch.setattr("company_corpus.cli.download_universe", fake_download)
    main(["--data-dir", str(tmp_path), "discover", "--ciks", "320193", "--download"])
    assert captured["config"].data_dir == tmp_path


def test_discover_download_without_years_passes_no_window(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("company_corpus.cli.discover_universe",
                        lambda ciks, **kw: _disc_report())

    def fake_download(ciks, **kw):
        captured.update(kw)
        return _dl_report()

    monkeypatch.setattr("company_corpus.cli.download_universe", fake_download)
    main(["discover", "--ciks", "320193", "--download"])
    # No period flags -> no implicit 20-year cap on the download step.
    assert captured["year_min"] is None and captured["year_max"] is None
    assert captured["since"] is None and captured["until"] is None


def test_discover_download_threads_period_flags(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("company_corpus.cli.discover_universe",
                        lambda ciks, **kw: _disc_report())

    def fake_download(ciks, **kw):
        captured.update(kw)
        return _dl_report()

    monkeypatch.setattr("company_corpus.cli.download_universe", fake_download)
    main(["discover", "--ciks", "320193", "--download",
          "--years", "2015-2018", "--since", "2016-06-01"])
    assert captured["year_min"] == 2015 and captured["year_max"] == 2018
    assert captured["since"] == date(2016, 6, 1)


def test_xbrl_years_passes_both_bounds(monkeypatch):
    captured: dict = {}

    def fake_fetch(ciks, **kw):
        captured.update(kw)
        return types.SimpleNamespace(issuers=1, periods=0, stats=_stats(), errors=[])

    monkeypatch.setattr("company_corpus.cli.fetch_financials", fake_fetch)
    main(["xbrl", "--ciks", "320193", "--years", "2015-2018"])
    # --years is a real range now, not a lower bound only.
    assert captured["since_year"] == 2015 and captured["until_year"] == 2018


def _patch_ticker_table(monkeypatch):
    table = {
        "AAPL": Issuer(cik="320193", ticker="AAPL", company="Apple Inc."),
        "DT": Issuer(cik="1773383", ticker="DT", company="Dynatrace, Inc."),
    }
    monkeypatch.setattr("company_corpus.cli.load_company_tickers", lambda fetcher: table)


def _write_bonds_and_crosswalk(tmp_path):
    bonds = tmp_path / "u.csv"
    bonds.write_text(
        "Ticker,CUSIP,Issuer\n"
        "AAPL,037833AA0,Apple Inc\n"
        "DT,25156PAA0,Deutsche Telekom Intl\n",
        encoding="utf-8",
    )
    xw = tmp_path / "xw.csv"
    xw.write_text("cik,cusip6,cusip8\n320193.0,037833,03783310\n999999.0,25156P,25156P10\n",
                  encoding="utf-8")
    return bonds, xw


def test_build_universe_from_file_keeps_collision_preferring_cusip(monkeypatch, tmp_path):
    _patch_ticker_table(monkeypatch)
    bonds, xw = _write_bonds_and_crosswalk(tmp_path)
    rc = main(["--data-dir", str(tmp_path / "data"), "build-universe",
               "--from-file", str(bonds), "--crosswalk", str(xw), "--name", "u", "--write"])
    assert rc == 0
    cfg = Config(data_dir=tmp_path / "data")
    by_ticker = {i.ticker: i for i in Universe(cfg).load("u")}
    assert by_ticker["AAPL"].cik == "0000320193"
    assert by_ticker["DT"].cik == "0000999999"
    assert by_ticker["DT"].resolution.startswith("collision")
    coll = Universe(cfg).path("u").with_name("u_collisions.jsonl")
    rows = [json.loads(line) for line in coll.read_text().splitlines() if line.strip()]
    assert rows[0]["ticker"] == "DT" and rows[0]["kind"] == "name_mismatch"


def test_build_universe_from_file_drop_collisions(monkeypatch, tmp_path):
    _patch_ticker_table(monkeypatch)
    bonds, xw = _write_bonds_and_crosswalk(tmp_path)
    rc = main(["--data-dir", str(tmp_path / "data"), "build-universe", "--from-file",
               str(bonds), "--crosswalk", str(xw), "--name", "u", "--drop-collisions", "--write"])
    assert rc == 0
    cfg = Config(data_dir=tmp_path / "data")
    tickers = {i.ticker for i in Universe(cfg).load("u")}
    assert "AAPL" in tickers and "DT" not in tickers


def test_build_universe_from_file_warns_without_crosswalk(monkeypatch, tmp_path, capsys):
    _patch_ticker_table(monkeypatch)
    bonds = tmp_path / "u.csv"
    bonds.write_text("Ticker,CUSIP\nAAPL,037833AA0\n", encoding="utf-8")
    rc = main(["--data-dir", str(tmp_path / "data"), "build-universe",
               "--from-file", str(bonds), "--name", "u", "--write"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no --crosswalk" in err.lower() or "without a crosswalk" in err.lower()
    cfg = Config(data_dir=tmp_path / "data")
    assert {i.ticker for i in Universe(cfg).load("u")} == {"AAPL"}


def test_equity_index_flag_builds_sp500(monkeypatch, tmp_path):
    monkeypatch.setattr("company_corpus.cli.issuers_from_sp500",
                        lambda fetcher, **kw: ([Issuer(cik="320193", ticker="AAPL")], [], []))
    rc = main(["--data-dir", str(tmp_path / "data"), "build-universe",
               "--equity-index", "sp500", "--current-only", "--write"])
    assert rc == 0
    cfg = Config(data_dir=tmp_path / "data")
    assert [i.ticker for i in Universe(cfg).load("sp500")] == ["AAPL"]


def test_legacy_index_alias_still_works_with_deprecation_notice(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("company_corpus.cli.issuers_from_sp500",
                        lambda fetcher, **kw: ([Issuer(cik="320193", ticker="AAPL")], [], []))
    rc = main(["--data-dir", str(tmp_path / "data"), "build-universe",
               "--index", "sp500", "--current-only", "--write"])
    assert rc == 0
    assert "deprecated" in capsys.readouterr().err.lower()
    cfg = Config(data_dir=tmp_path / "data")
    assert [i.ticker for i in Universe(cfg).load("sp500")] == ["AAPL"]


class _FakeFTS:
    instantiated = 0

    def __init__(self, *a, **k):
        type(self).instantiated += 1

    def resolve(self, cusip):
        return ("0000999999", "DEUTSCHE TELEKOM INTL FIN") if cusip == "25156PAA0" else None


def test_from_file_fts_resolves_unresolved(monkeypatch, tmp_path):
    monkeypatch.setattr("company_corpus.cli.load_company_tickers", lambda fetcher: {})
    monkeypatch.setattr("company_corpus.cli.EdgarFTS", _FakeFTS)
    bonds = tmp_path / "u.csv"
    bonds.write_text("Ticker,CUSIP,Issuer\nDT,25156PAA0,Deutsche Telekom Intl Finance\n",
                     encoding="utf-8")
    rc = main(["--data-dir", str(tmp_path / "data"), "build-universe",
               "--from-file", str(bonds), "--name", "u", "--fts", "--write"])
    assert rc == 0
    cfg = Config(data_dir=tmp_path / "data")
    by_ticker = {i.ticker: i for i in Universe(cfg).load("u")}
    assert by_ticker["DT"].cik == "0000999999"
    assert by_ticker["DT"].resolution == "fts:confirmed"


def test_from_file_without_fts_never_constructs_edgarfts(monkeypatch, tmp_path):
    monkeypatch.setattr("company_corpus.cli.load_company_tickers", lambda fetcher: {})

    def _boom(*a, **k):
        raise AssertionError("EdgarFTS must not be constructed without --fts")

    monkeypatch.setattr("company_corpus.cli.EdgarFTS", _boom)
    bonds = tmp_path / "u.csv"
    bonds.write_text("Ticker,CUSIP\nDT,25156PAA0\n", encoding="utf-8")
    rc = main(["--data-dir", str(tmp_path / "data"), "build-universe",
               "--from-file", str(bonds), "--name", "u", "--write"])
    assert rc == 0
    cfg = Config(data_dir=tmp_path / "data")
    assert "DT" not in {i.ticker for i in Universe(cfg).load("u")}


def test_enrich_openfigi_writes_csv(monkeypatch, tmp_path):
    from company_corpus.openfigi import FigiRecord
    src = tmp_path / "ids.csv"
    src.write_text("Ticker,ISIN\nABBNVX,US00037BAC63\nXYZ,USNOPE0000000\n", encoding="utf-8")
    fake = {
        "US00037BAC63": FigiRecord(name="ABB FINANCE USA INC", ticker="ABBNVX 4.375",
                                   security_type="GLOBAL", exch_code="TRACE"),
        "USNOPE0000000": None,
    }
    monkeypatch.setattr("company_corpus.cli.map_identifiers", lambda values, **kw: fake)
    out = tmp_path / "enriched.csv"
    rc = main(["enrich-openfigi", "--from-file", str(src), "--out", str(out)])
    assert rc == 0
    text = out.read_text()
    assert text.splitlines()[0] == "identifier,name,ticker,security_type,exch_code,coverage_hint"
    assert "US00037BAC63,ABB FINANCE USA INC,ABBNVX 4.375,GLOBAL,TRACE,registry_candidate" in text
    assert "USNOPE0000000,,,,,no_match" in text


def test_enrich_openfigi_uses_env_api_key(monkeypatch, tmp_path):
    captured = {}

    def fake_map(values, **kw):
        captured.update(kw)
        return {v: None for v in values}

    monkeypatch.setenv("OPENFIGI_API_KEY", "envkey")
    monkeypatch.setattr("company_corpus.cli.map_identifiers", fake_map)
    src = tmp_path / "ids.csv"
    src.write_text("ISIN\nUS00037BAC63\n", encoding="utf-8")
    rc = main(["enrich-openfigi", "--from-file", str(src)])
    assert rc == 0
    assert captured["api_key"] == "envkey"


def test_build_universe_from_file_name_tier(tmp_path, monkeypatch, capsys):
    # A row whose ticker doesn't resolve falls through to the name tier; the
    # default ledger is written under data/reference/. The ticker column is
    # required (read_identifier_csv needs a CIK/Ticker/CUSIP column); the ticker
    # table is stubbed empty so ticker resolution misses and never hits network.
    monkeypatch.setattr("company_corpus.cli.load_company_tickers",
                        lambda fetcher: {})
    monkeypatch.setattr("company_corpus.cli.fetch_cik_lookup",
                        lambda fetcher, path: "WIDGET INC:0000999999:\n")

    csv_path = tmp_path / "names.csv"
    csv_path.write_text("Ticker,Name\nZZZZ,Widget Inc\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    rc = main(["--data-dir", str(data_dir), "build-universe",
               "--from-file", str(csv_path), "--name", "names", "--write"])
    assert rc == 0
    ledger = data_dir / "reference" / "name_cik_cache.csv"
    assert ledger.exists()
    assert "WIDGET" in ledger.read_text(encoding="utf-8")
    out = (data_dir / "universe" / "names.jsonl").read_text(encoding="utf-8")
    assert "0000999999" in out


def test_build_universe_from_file_no_name_resolution(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("company_corpus.cli.load_company_tickers",
                        lambda fetcher: {})
    called = {"n": 0}
    def _boom(fetcher, path):
        called["n"] += 1
        return ""
    monkeypatch.setattr("company_corpus.cli.fetch_cik_lookup", _boom)

    csv_path = tmp_path / "names.csv"
    csv_path.write_text("Ticker,Name\nZZZZ,Widget Inc\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    rc = main(["--data-dir", str(data_dir), "build-universe",
               "--from-file", str(csv_path), "--name", "names",
               "--no-name-resolution"])
    assert rc == 0
    assert called["n"] == 0  # tier disabled -> the lookup file is never fetched
    assert not (data_dir / "reference" / "name_cik_cache.csv").exists()


class _BoomFTS:
    """An EdgarFTS stand-in whose resolve() must never be called."""

    def __init__(self, *a, **k):
        pass

    def resolve(self, cusip):
        raise AssertionError("fts.resolve called for a cached CUSIP")


def test_fts_cache_read_skips_fts(monkeypatch, tmp_path):
    monkeypatch.setattr("company_corpus.cli.load_company_tickers", lambda fetcher: {})
    monkeypatch.setattr("company_corpus.cli.EdgarFTS", _BoomFTS)
    bonds = tmp_path / "u.csv"
    bonds.write_text("Ticker,CUSIP\nTKM,25156PAA0\n", encoding="utf-8")
    cache = tmp_path / "cache.csv"
    cache.write_text("cik,cusip6\n0000999999,25156P\n", encoding="utf-8")
    rc = main(["--data-dir", str(tmp_path / "data"), "build-universe",
               "--from-file", str(bonds), "--name", "u", "--fts", "--fts-cache", str(cache), "--write"])
    assert rc == 0
    cfg = Config(data_dir=tmp_path / "data")
    by_ticker = {i.ticker: i for i in Universe(cfg).load("u")}
    assert by_ticker["TKM"].cik == "0000999999"
    assert by_ticker["TKM"].resolution == "cusip"


class _OneHitFTS:
    """Confirmed hit for 25156PAA0; mismatched (unverified) hit for 88888XAA0."""

    def __init__(self, *a, **k):
        pass

    def resolve(self, cusip):
        if cusip == "25156PAA0":
            return ("0000999999", "DEUTSCHE TELEKOM INTL FIN")
        if cusip == "88888XAA0":
            return ("0000888888", "PPLUS TRUST SERIES DCNA-1")  # name mismatch -> unverified
        return None


def test_fts_cache_writes_confirmed_only_without_write_flag(monkeypatch, tmp_path):
    monkeypatch.setattr("company_corpus.cli.load_company_tickers", lambda fetcher: {})
    monkeypatch.setattr("company_corpus.cli.EdgarFTS", _OneHitFTS)
    bonds = tmp_path / "u.csv"
    bonds.write_text(
        "Ticker,CUSIP,Issuer\n"
        "TKM,25156PAA0,Deutsche Telekom Intl Finance\n"
        "FOO,88888XAA0,Foo Industries\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache.csv"
    rc = main(["--data-dir", str(tmp_path / "data"), "build-universe",
               "--from-file", str(bonds), "--name", "u", "--fts", "--fts-cache", str(cache)])
    assert rc == 0
    from company_corpus.universe import load_cusip_crosswalk
    xw = load_cusip_crosswalk(cache)
    assert xw == {"25156P": {"0000999999"}}
    assert "88888X" not in xw
    assert not (tmp_path / "data" / "universe" / "u.jsonl").exists()


def test_name_tier_fetch_failure_degrades_gracefully(monkeypatch, tmp_path, capsys):
    """A transient SEC fetch failure in _name_tier must not abort build-universe.

    rc must be 0, stderr must carry the WARNING, and the build must complete
    (even though no name resolution happens). Overrides the autouse
    _stub_name_fetch with a raising stub set inside the test body.
    """
    monkeypatch.setattr("company_corpus.cli.load_company_tickers", lambda fetcher: {})
    # Override the autouse stub with one that raises.
    monkeypatch.setattr(
        "company_corpus.cli.fetch_cik_lookup",
        lambda fetcher, path: (_ for _ in ()).throw(
            RuntimeError("simulated network failure")),
    )

    csv_path = tmp_path / "names.csv"
    csv_path.write_text("Ticker,Name\nZZZZ,Widget Inc\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    rc = main(["--data-dir", str(data_dir), "build-universe",
               "--from-file", str(csv_path), "--name", "names"])
    assert rc == 0, "build-universe must exit 0 even when the name-tier fetch fails"
    err = capsys.readouterr().err
    assert "WARNING" in err, f"expected a WARNING on stderr; got: {err!r}"
    assert "cik-lookup fetch failed" in err, (
        f"stderr should mention the failure reason; got: {err!r}"
    )
