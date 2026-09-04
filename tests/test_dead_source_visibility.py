"""A dead source must never look like "the issuer filed nothing".

Three layers are asserted here, one per silent-failure site the doctrine closes:

* the register API modules log a swallowed fetch failure at WARNING (it used to
  be DEBUG, i.e. invisible under the CLI's INFO root logger);
* a producer that could not reach its source emits a ``source-error`` coverage
  row (never ``no-financials``), counts it in ``out["source_errors"]`` and
  appends a timestamped ``out["error_items"]`` entry;
* the CLI folds those items into the run report (degraded / exit 3 when no
  useful work came out) and appends them to ``discovery_errors.jsonl``, stamped
  with the run id.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

import pytest

from company_corpus import cli
from company_corpus.config import Config
from company_corpus.storage import Storage


class _RaisingFetcher:
    """Every call fails — a source that is dead, not empty."""

    def get(self, url, *args, **kw):
        raise RuntimeError("simulated network error")

    def get_json(self, url, *args, **kw):
        raise RuntimeError("simulated network error")

    def post_json(self, url, *args, **kw):
        raise RuntimeError("simulated network error")


# --------------------------------------------------------------------------
# (a) the register API modules warn — one per module
# --------------------------------------------------------------------------
def test_bnb_cbso_fetch_failure_warns(caplog):
    from company_corpus.registers.bnb_cbso import fetch_bnb_deposit

    with caplog.at_level(logging.WARNING):
        assert fetch_bnb_deposit("0403170701", fetcher=_RaisingFetcher(), key="k") is None
    assert [r for r in caplog.records if r.levelno == logging.WARNING], caplog.text


def test_virk_search_failure_warns(caplog):
    from company_corpus.registers.virk_api import search_virk_filings

    with caplog.at_level(logging.WARNING):
        assert search_virk_filings("24256790", fetcher=_RaisingFetcher()) == []
    assert [r for r in caplog.records if r.levelno == logging.WARNING], caplog.text


def test_prh_list_dates_failure_warns(caplog):
    from company_corpus.registers.prh_api import list_fi_dates

    with caplog.at_level(logging.WARNING):
        assert list_fi_dates("2919415-2", fetcher=_RaisingFetcher()) == []
    assert [r for r in caplog.records if r.levelno == logging.WARNING], caplog.text


def test_sk_fetch_vykaz_failure_warns(caplog):
    from company_corpus.registers.sk_registeruz import fetch_vykaz

    with caplog.at_level(logging.WARNING):
        assert fetch_vykaz(1, fetcher=_RaisingFetcher()) is None
    assert [r for r in caplog.records if r.levelno == logging.WARNING], caplog.text


# --------------------------------------------------------------------------
# (b) register producers: a dead source is a source-error, not no-financials
# --------------------------------------------------------------------------
class _FiDeadDocumentFetcher:
    """PRH lists a filing for the issuer (the source is alive and says a filing
    exists) but the document GET fails — the fetch returned an explicit failure,
    so the issuer must NOT be recorded as "filed nothing"."""

    def __init__(self, *a, **kw):
        pass

    def get_json(self, url, *, headers=None, params=None, **kw):
        return {"totalResults": 1,
                "financials": [{"businessId": "2919415-2",
                                "financialDate": "2024-12-31"}]}

    def get(self, url, *, headers=None, params=None, **kw):
        raise RuntimeError("PRH 503")


def _coverage(tmp_path, source: str) -> list[dict]:
    path = tmp_path / "reports" / f"register_coverage_{source}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if x]


def test_fi_producer_marks_dead_source(tmp_path):
    from company_corpus.registers.financials import build_fi_financials

    cfg = Config(data_dir=tmp_path)
    out = build_fi_financials([{"business_id": "2919415-2"}],
                              fetcher=_FiDeadDocumentFetcher(), config=cfg, write=True)

    assert out["no_financials"] == 0, "a dead fetch must not be counted as 'filed nothing'"
    assert out["errors"] == 1
    assert out["source_errors"] == 1
    item = out["error_items"][0]
    assert item["entity_id"] == "2919415-2" and item["source"] == "prh"
    assert item["error"] and datetime.fromisoformat(item["ts"])

    cov = _coverage(tmp_path, "prh")[0]
    assert cov["status"] == "source-error" and cov["error"]


def test_register_producer_exception_is_source_error(tmp_path):
    """An exception reaching a producer is a source-error too (was ``error``)."""
    from company_corpus.registers.financials import build_register_financials

    class _BadBrreg:
        def get_json(self, url, *a, **kw):
            return [{"regnskapsperiode": "not-a-dict"}]

    cfg = Config(data_dir=tmp_path)
    out = build_register_financials([{"orgnr": "100000000"}],
                                    fetcher=_BadBrreg(), config=cfg, write=True)
    assert out["errors"] == 1 and out["source_errors"] == 1
    assert out["error_items"][0]["entity_id"] == "100000000"
    assert _coverage(tmp_path, "brreg")[0]["status"] == "source-error"


def test_register_cli_dead_source_is_degraded_and_logged(monkeypatch, tmp_path):
    """End to end: PRH's document fetch dies -> zero periods + one fetch error =>
    degraded (exit 3), a non-empty error sample, and a discovery_errors row
    stamped with the run id."""
    monkeypatch.setattr(cli, "Fetcher", _FiDeadDocumentFetcher)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["--data-dir", str(tmp_path), "register-financials",
                   "--fi-businessid", "2919415-2", "--write"])
    rep = json.loads((tmp_path / "runs.jsonl").read_text().strip().split("\n")[-1])

    assert rc == 3 and rep["outcome"] == "degraded"
    src = [s for s in rep["sources"] if s["source_code"] == "prh"][0]
    assert src["fetch_errors"] == 1 and src["error_samples"]

    rows = [json.loads(x) for x in
            (tmp_path / "discovery_errors.jsonl").read_text().splitlines() if x]
    assert rows and rows[-1]["run_id"] == rep["run_id"]
    assert datetime.fromisoformat(rows[-1]["ts"])


# --------------------------------------------------------------------------
# (c) EU pillar A: reconcile + acquire
# --------------------------------------------------------------------------
def test_reconcile_flags_source_error_over_no_documents():
    from company_corpus.eu.entities import Entity
    from company_corpus.eu.reconcile import reconcile

    ents = [Entity("LEI1", "A", "DE", resolution="lei"),
            Entity("LEI2", "B", "DE", resolution="lei")]
    rows = {r["lei"]: r for r in reconcile(ents, [], errors={"LEI1": "boom"})}
    assert rows["LEI1"]["gap"] == "source-error" and rows["LEI1"]["error"] == "boom"
    assert rows["LEI2"]["gap"] == "no-documents"


def test_acquire_backend_failure_surfaces_in_coverage(monkeypatch, tmp_path):
    from company_corpus.eu import acquire as acq
    from company_corpus.eu.entities import Entity

    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    ent = Entity("LEI1", "SAP SE", "DE", resolution="lei")
    monkeypatch.setattr(acq, "resolve_entities", lambda specs, *, fetcher: [ent])

    class _DeadBackend:
        def __init__(self, *a, **k):
            self.errors = []

        def discover(self, e):
            raise RuntimeError("OAM down")

    class _EmptyBackend:
        def __init__(self, *a, **k):
            self.errors = []

        def discover(self, e):
            return []

    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"DE": _DeadBackend})
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _EmptyBackend)

    summary = acq.acquire([{"lei": "LEI1"}], fetcher=object(), config=cfg, download=False)
    assert len(summary["errors"]) == 1
    assert summary["error_items"] == summary["errors"]
    cov = [json.loads(x) for x in
           (cfg.data_dir / "reports" / "eu_coverage.jsonl").read_text().splitlines() if x]
    assert cov[0]["gap"] == "source-error" and "OAM down" in cov[0]["error"]


# --------------------------------------------------------------------------
# (d) EU pillar B: eu-financials
# --------------------------------------------------------------------------
def _esef_doc():
    from company_corpus.eu.documents import Document

    return Document("f1", "LEI1", "DE", "annual_report", date(2023, 12, 31),
                    "2024-04-01", "x", "en", "filings.xbrl.org",
                    [{"kind": "json_url", "url": "https://x/report.json"}], {})


@pytest.fixture()
def _dead_esef(monkeypatch):
    """filings.xbrl.org lists one filing whose facts JSON cannot be fetched."""
    from company_corpus.eu import financials as euf
    from company_corpus.eu.entities import Entity

    class _Src:
        def __init__(self, *a, **k):
            self.errors = []  # the backend contract: recorded (swallowed) errors

        def discover(self, entity):
            return [_esef_doc()]

    monkeypatch.setattr(euf, "FilingsXbrlOrg", _Src)
    monkeypatch.setattr(euf, "resolve_entities",
                        lambda specs, *, fetcher: [Entity("LEI1", "A", "DE", resolution="lei")])
    return _RaisingFetcher()


def test_eu_financials_counts_dead_filing(tmp_path, _dead_esef):
    from company_corpus.eu.financials import build_eu_financials

    cfg = Config(data_dir=tmp_path)
    out = build_eu_financials([{"lei": "LEI1"}], fetcher=_dead_esef, config=cfg, write=True)
    assert out["errors"] == 1
    item = out["error_items"][0]
    assert item["entity_id"] == "LEI1" and item["source"] == "esef"
    assert item["error"] and datetime.fromisoformat(item["ts"])


def test_eu_financials_cli_degraded_and_logged(monkeypatch, tmp_path, _dead_esef):
    monkeypatch.setattr(cli, "Fetcher", lambda cfg: _dead_esef)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["--data-dir", str(tmp_path), "eu-financials", "--leis", "LEI1", "--write"])
    rep = json.loads((tmp_path / "runs.jsonl").read_text().strip().split("\n")[-1])

    assert rc == 3 and rep["outcome"] == "degraded"
    assert [s for s in rep["sources"] if s["source_code"] == "xbrlorg"][0]["error_samples"]
    rows = [json.loads(x) for x in
            (tmp_path / "discovery_errors.jsonl").read_text().splitlines() if x]
    assert rows and rows[-1]["run_id"] == rep["run_id"]


@pytest.fixture()
def _dead_esef_listing(monkeypatch):
    """filings.xbrl.org itself is unreachable: the per-entity filings LISTING
    fails. The real backend swallows that into ``src.errors`` and returns [] —
    which must not read as "the issuer filed nothing"."""
    from company_corpus.eu import financials as euf
    from company_corpus.eu.entities import Entity

    monkeypatch.setattr(euf, "resolve_entities",
                        lambda specs, *, fetcher: [Entity("LEI1", "A", "DE", resolution="lei")])
    return _RaisingFetcher()


def test_eu_financials_dead_listing_is_source_error(tmp_path, _dead_esef_listing):
    """Mirror of the register 'first call failure is source-error' test for the
    aggregator: a dead LISTING is the aggregator's failure, never no-financials."""
    from company_corpus.eu.financials import build_eu_financials

    out = build_eu_financials([{"lei": "LEI1"}], fetcher=_dead_esef_listing,
                              config=Config(data_dir=tmp_path), write=True)
    assert out["entities"] == 1
    assert out["no_financials"] == 0, "a dead aggregator must not read as 'filed nothing'"
    assert out["errors"] == 1
    item = out["error_items"][0]
    assert item["entity_id"] == "LEI1" and item["source"] == "esef"
    assert "discover" in item["error"] and "simulated network error" in item["error"]
    cov = [json.loads(x) for x in
           (tmp_path / "reports" / "eu_financials_coverage.jsonl").read_text().splitlines() if x]
    assert cov[0]["status"] == "source-error" and "simulated network error" in cov[0]["error"]


def test_eu_financials_cli_dead_listing_is_degraded(monkeypatch, tmp_path, _dead_esef_listing):
    monkeypatch.setattr(cli, "Fetcher", lambda cfg: _dead_esef_listing)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["--data-dir", str(tmp_path), "eu-financials", "--leis", "LEI1", "--write"])
    rep = json.loads((tmp_path / "runs.jsonl").read_text().strip().split("\n")[-1])

    assert rc == 3 and rep["outcome"] == "degraded"
    src = [s for s in rep["sources"] if s["source_code"] == "xbrlorg"][0]
    assert src["docs_failed"] == 1 and src["error_samples"]
    rows = [json.loads(x) for x in
            (tmp_path / "discovery_errors.jsonl").read_text().splitlines() if x]
    assert rows and rows[-1]["run_id"] == rep["run_id"]


# --------------------------------------------------------------------------
# (d') GLEIF itself is dead: every spec is unresolved -> a failed run, not a
#      green "no financials"
# --------------------------------------------------------------------------
def test_eu_financials_counts_unresolved_specs(tmp_path):
    """GLEIF unreachable maps every LEI spec to 'unresolved'; the producer
    counts them and lists the input specs so the CLI can tell the difference
    between 'filed nothing' and 'could not even resolve'."""
    from company_corpus.eu.financials import build_eu_financials

    out = build_eu_financials([{"lei": "LEI1"}, {"lei": "LEI2"}], fetcher=_RaisingFetcher(),
                              config=Config(data_dir=tmp_path), write=False)
    assert out["entities"] == 2 and out["with_financials"] == 0
    assert out["unresolved"] == 2
    assert out["unresolved_specs"] == [{"lei": "LEI1"}, {"lei": "LEI2"}]


def test_register_financials_counts_unresolved_specs(tmp_path):
    from company_corpus.registers.financials import build_register_financials

    out = build_register_financials([{"lei": "LEI1"}], fetcher=_RaisingFetcher(),
                                    config=Config(data_dir=tmp_path), write=False)
    assert out["entities"] == 1 and out["unresolved"] == 1
    assert out["unresolved_specs"] == [{"lei": "LEI1"}]


@pytest.mark.parametrize("argv", [
    ["eu-financials", "--leis", "LEI1,LEI2"],
    ["register-financials", "--leis", "LEI1,LEI2"],
])
def test_cli_dead_gleif_every_spec_unresolved_is_failed(argv, monkeypatch, tmp_path, capsys):
    """Dead GLEIF: nothing resolves, no source is queried, and the run would
    pass for a green nothing-to-do. It fails instead (exit 1 via the return-2
    folding), naming GLEIF as a possible cause, and lists the specs."""
    monkeypatch.setattr(cli, "Fetcher", lambda cfg: _RaisingFetcher())
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["--data-dir", str(tmp_path), *argv])
    rep = json.loads((tmp_path / "runs.jsonl").read_text().strip().split("\n")[-1])
    assert rc == 1 and rep["outcome"] == "failed"
    captured = capsys.readouterr()
    assert "unresolved" in captured.err and "GLEIF" in captured.err
    assert "unresolved: LEI LEI1, LEI LEI2" in captured.out


def test_eu_financials_cli_prints_partially_unresolved_specs(monkeypatch, tmp_path, capsys):
    """One of two specs unresolved: the run still reports the resolved one
    normally and prints the unresolved spec — exit 0, never a silent drop."""
    monkeypatch.setattr(cli, "build_eu_financials", lambda specs, **kw: {
        "entities": 2, "with_financials": 1, "no_financials": 1, "periods": 3,
        "paths": [], "errors": 0, "error_items": [], "coverage_path": None,
        "unresolved": 1, "unresolved_specs": [{"lei": "L9"}]})
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["eu-financials", "--leis", "L1,L9"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "unresolved: LEI L9" in captured.out and "error" not in captured.err


# --------------------------------------------------------------------------
# (e) the error trail itself
# --------------------------------------------------------------------------
def test_record_errors_stamps_ts_and_run_id(tmp_path):
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    assert st.record_errors([{"source": "x", "error": "boom"}], run_id="r1") == 1
    assert st.record_errors([{"source": "y", "error": "bang"}]) == 1
    rows = [json.loads(x) for x in cfg.discovery_errors_path.read_text().splitlines() if x]
    assert rows[0]["run_id"] == "r1" and datetime.fromisoformat(rows[0]["ts"])
    assert rows[0]["source"] == "x"
    assert rows[1].get("run_id") is None and datetime.fromisoformat(rows[1]["ts"])


def test_record_errors_preserves_existing_stamps(tmp_path):
    cfg = Config(data_dir=tmp_path)
    Storage(cfg).record_errors(
        [{"error": "boom", "ts": "2020-01-01T00:00:00+00:00", "run_id": "own"}], run_id="r1")
    row = json.loads(cfg.discovery_errors_path.read_text().splitlines()[0])
    assert row["ts"] == "2020-01-01T00:00:00+00:00" and row["run_id"] == "own"


# --------------------------------------------------------------------------
# (f) SEC pillar: every pipeline trail row carries the run id
# --------------------------------------------------------------------------
@pytest.fixture()
def _captured_trail(monkeypatch):
    """Capture ``Storage.record_errors`` calls (rows + run_id) instead of writing."""
    calls: list[dict] = []

    def _fake(self, errors, *, run_id=None):
        calls.append({"rows": list(errors), "run_id": run_id})
        return len(errors)

    monkeypatch.setattr(Storage, "record_errors", _fake)
    return calls


def _seed_sec_record(config, **kw):
    from company_corpus.models import FilingRecord
    from company_corpus.taxonomy import FormType

    st = Storage(config)
    rec = FilingRecord(cik="320193", form_type=kw.pop("form_type", FormType.A1),
                       sec_form=kw.pop("sec_form", "10-K"), accession="0000320193-24-000123",
                       company="Apple Inc.", filing_date=date(2024, 11, 1),
                       submission_url="https://sec/0000320193-24-000123.txt", **kw)
    return st, rec


def _run_discover(config, make_fetcher, run_id):
    from company_corpus.pipeline import discover_universe

    discover_universe(["999999"], dry_run=False, config=config,
                      fetcher=make_fetcher({}), run_id=run_id)


def _run_download(config, make_fetcher, run_id):
    from company_corpus.pipeline import download_universe

    st, rec = _seed_sec_record(config)
    st.save_records([rec], dry_run=False)
    download_universe(["320193"], dry_run=False, config=config,
                      fetcher=make_fetcher({}), storage=st, run_id=run_id)


def _run_render(config, make_fetcher, run_id):
    from company_corpus.pipeline import render_universe

    st, rec = _seed_sec_record(config)
    dest = st.raw_dir_for(rec)
    dest.mkdir(parents=True, exist_ok=True)
    primary = dest / f"{rec.doc_id}.primary.htm"
    primary.write_text("<html><body>hi</body></html>", encoding="utf-8")
    rec.primary_path = str(primary.relative_to(config.data_dir))
    st.save_records([rec], dry_run=False)

    def _boom(src, dst):
        raise RuntimeError("chrome failed")

    render_universe(["320193"], renderer=_boom, dry_run=False, config=config,
                    storage=st, run_id=run_id)


def _run_xbrl(config, make_fetcher, run_id):
    from company_corpus.pipeline import fetch_financials

    fetch_financials(["320193"], dry_run=False, config=config,
                     fetcher=make_fetcher({}), run_id=run_id)


def _run_ownership(config, make_fetcher, run_id):
    from company_corpus.pipeline import process_ownership
    from company_corpus.taxonomy import FormType

    st, rec = _seed_sec_record(config, form_type=FormType.E1, sec_form="4")
    st.save_records([rec], dry_run=False)
    process_ownership(["320193"], dry_run=False, config=config,
                      fetcher=make_fetcher({}), storage=st, run_id=run_id)


@pytest.mark.parametrize("runner", [
    _run_discover, _run_download, _run_render, _run_xbrl, _run_ownership,
], ids=["discover", "download", "render", "xbrl", "ownership"])
def test_sec_pipeline_trail_rows_carry_run_id(runner, config, make_fetcher, _captured_trail):
    """A dead SEC fetch leaves a trail row stamped with the run that hit it."""
    runner(config, make_fetcher, "run-sec-1")
    assert _captured_trail, "the failure must reach the error trail"
    assert all(c["run_id"] == "run-sec-1" and c["rows"] for c in _captured_trail)


def _ns(**kw):
    import types

    return types.SimpleNamespace(**kw)


def _zero_stats():
    return _ns(seen=0, added=0, updated=0, unchanged=0)


_SEC_CLI_CASES = {
    "discover": ("discover_universe",
                 lambda: _ns(issuers=1, rounds=1, stats=_zero_stats(), errors=[])),
    "download": ("download_universe",
                 lambda: _ns(downloaded=0, skipped=0, empty=0, errors=0, bytes=0,
                             error_items=[])),
    "render-pdf": ("render_universe",
                   lambda: _ns(rendered=0, would_render=0, skipped=0, no_primary=0,
                               errors=0, error_items=[])),
    "xbrl": ("fetch_financials",
             lambda: _ns(issuers=1, periods=0, stats=_zero_stats(), errors=[])),
    "ownership": ("process_ownership",
                  lambda: _ns(issuers=1, downloaded=0, parsed_insider=0, parsed_13f=0,
                              passthrough=0, errors=0, error_items=[])),
}


@pytest.mark.parametrize("cmd", sorted(_SEC_CLI_CASES))
def test_sec_cli_threads_run_id_into_pipeline(cmd, monkeypatch, tmp_path):
    """Each SEC command hands the run report's id to its pipeline entry point."""
    func_name, make_report = _SEC_CLI_CASES[cmd]
    seen: dict = {}

    def _fake(*a, **kw):
        seen.update(kw)
        return make_report()

    monkeypatch.setattr(cli, func_name, _fake)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    cli.main(["--data-dir", str(tmp_path), cmd, "--ciks", "320193", "--write"])
    rep = json.loads((tmp_path / "runs.jsonl").read_text().strip().split("\n")[-1])
    assert "run_id" in seen, f"{func_name} was not given run_id="
    assert seen["run_id"] == rep["run_id"]


# --------------------------------------------------------------------------
# (g) the trail is corpus state too: a dry run leaves none
# --------------------------------------------------------------------------
def test_register_cli_dry_run_writes_no_trail(monkeypatch, tmp_path):
    """Without --write the run is still degraded (the errors happened) but
    nothing lands on disk — the SEC pillar gates its trail the same way."""
    monkeypatch.setattr(cli, "Fetcher", _FiDeadDocumentFetcher)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["--data-dir", str(tmp_path), "register-financials",
                   "--fi-businessid", "2919415-2"])
    rep = json.loads((tmp_path / "runs.jsonl").read_text().strip().split("\n")[-1])
    assert rc == 3 and rep["outcome"] == "degraded"
    assert not (tmp_path / "discovery_errors.jsonl").exists()


def test_eu_financials_cli_dry_run_writes_no_trail(monkeypatch, tmp_path, _dead_esef):
    monkeypatch.setattr(cli, "Fetcher", lambda cfg: _dead_esef)
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["--data-dir", str(tmp_path), "eu-financials", "--leis", "LEI1"])
    rep = json.loads((tmp_path / "runs.jsonl").read_text().strip().split("\n")[-1])
    assert rc == 3 and rep["outcome"] == "degraded"
    assert not (tmp_path / "discovery_errors.jsonl").exists()


# --------------------------------------------------------------------------
# (h) register helpers: a failed FIRST call is a dead source, not an empty filer
# --------------------------------------------------------------------------
# The API helpers swallow exceptions into []/None by contract (batch-safe).
# Before this section, a producer whose very first register call died could not
# tell "dead" from "empty" and recorded no-financials with errors == 0. Each
# helper now takes an ``errors`` out-parameter; the producers read it.

def _build_no(cfg, fetcher):
    from company_corpus.registers.financials import build_register_financials

    return build_register_financials([{"orgnr": "923609016"}], fetcher=fetcher,
                                     config=cfg, write=True)


def _build_be(cfg, fetcher):
    from company_corpus.registers.financials import build_be_financials

    return build_be_financials([{"be_number": "0648822310"}], fetcher=fetcher,
                               config=cfg, key="k", write=True)


def _build_fi(cfg, fetcher):
    from company_corpus.registers.financials import build_fi_financials

    return build_fi_financials([{"business_id": "2919415-2"}], fetcher=fetcher,
                               config=cfg, write=True)


def _build_dk(cfg, fetcher):
    from company_corpus.registers.financials import build_dk_financials

    return build_dk_financials([{"cvr": "30830725"}], fetcher=fetcher,
                               config=cfg, write=True)


def _build_sk(cfg, fetcher):
    from company_corpus.registers.financials import build_sk_financials

    return build_sk_financials([9000014], fetcher=fetcher, config=cfg, write=True)


# (builder, coverage-file suffix, run-report source code, first helper called)
_REGISTER_CASES = {
    "no": (_build_no, "brreg", "brreg", "fetch_brreg_accounts"),
    "be": (_build_be, "bnb", "bnb", "fetch_bnb_deposit"),
    "fi": (_build_fi, "prh", "prh", "list_fi_dates"),
    "dk": (_build_dk, "erst", "erst", "search_virk_filings"),
    "sk": (_build_sk, "registeruz", "registeruz", "fetch_entity"),
}


class _EmptyHealthyFetcher:
    """A reachable register that simply lists nothing for the entity."""

    def get_json(self, url, *a, **kw):
        if "avoindata.prh.fi" in url:
            return {"totalResults": 0, "financials": []}
        if "registeruz.sk" in url:
            return {}
        return []  # brreg accounts / CBSO references

    def post_json(self, url, body, *a, **kw):
        return {"hits": {"hits": []}}

    def get(self, url, *a, **kw):
        raise AssertionError("no document should be fetched for an empty listing")


@pytest.mark.parametrize("reg", sorted(_REGISTER_CASES))
def test_register_first_call_failure_is_source_error(reg, tmp_path):
    build, cov_suffix, _code, stage = _REGISTER_CASES[reg]
    out = build(Config(data_dir=tmp_path), _RaisingFetcher())

    assert out["entities"] == 1
    assert out["no_financials"] == 0, "a dead register must not read as 'filed nothing'"
    assert out["errors"] == 1 and out["source_errors"] == 1
    item = out["error_items"][0]
    assert item["entity_id"] is not None and item["source"] == _code
    assert stage in item["error"] and "RuntimeError" in item["error"]
    cov = _coverage(tmp_path, cov_suffix)[0]
    assert cov["status"] == "source-error" and stage in cov["error"]


@pytest.mark.parametrize("reg", sorted(_REGISTER_CASES))
def test_register_empty_listing_on_healthy_source_stays_no_financials(reg, tmp_path):
    """The false-positive guard: an empty answer from a live register is still
    the issuer's own 'filed nothing'."""
    build, cov_suffix, _code, _stage = _REGISTER_CASES[reg]
    out = build(Config(data_dir=tmp_path), _EmptyHealthyFetcher())

    assert out["no_financials"] == 1
    assert out["errors"] == 0 and out["source_errors"] == 0 and not out["error_items"]
    assert _coverage(tmp_path, cov_suffix)[0]["status"] == "no-financials"


_REGISTER_CLI_FLAGS = {
    "no": ["--orgnrs", "923609016"],
    "be": ["--be-numbers", "0648822310"],
    "fi": ["--fi-businessid", "2919415-2"],
    "dk": ["--dk-cvr", "30830725"],
    "sk": ["--sk-id", "9000014"],
}


@pytest.mark.parametrize("reg", sorted(_REGISTER_CLI_FLAGS))
def test_register_cli_dead_first_call_is_degraded(reg, monkeypatch, tmp_path):
    """End to end: the register's first call dies -> degraded (exit 3), a
    non-empty error sample, and a trail row stamped with the run id."""
    monkeypatch.setattr(cli, "Fetcher", lambda cfg: _RaisingFetcher())
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BNB_CBSO_KEY", "dummy")
    rc = cli.main(["--data-dir", str(tmp_path), "register-financials",
                   *_REGISTER_CLI_FLAGS[reg], "--write"])
    rep = json.loads((tmp_path / "runs.jsonl").read_text().strip().split("\n")[-1])

    assert rc == 3 and rep["outcome"] == "degraded"
    # The run report keys sources by authority code (source_codes.py), which
    # is the producer tag everywhere but Belgium (bnb -> nbb).
    code = {"bnb": "nbb"}.get(_REGISTER_CASES[reg][2], _REGISTER_CASES[reg][2])
    src = [s for s in rep["sources"] if s["source_code"] == code][0]
    assert src["fetch_errors"] == 1 and src["error_samples"]
    rows = [json.loads(x) for x in
            (tmp_path / "discovery_errors.jsonl").read_text().splitlines() if x]
    assert rows and rows[-1]["run_id"] == rep["run_id"]


@pytest.mark.parametrize("helper, call", [
    ("no_brreg.fetch_brreg_accounts", lambda f, e: ("923609016",), ),
    ("bnb_cbso.fetch_bnb_deposit", lambda f, e: ("0648822310",)),
    ("virk_api.search_virk_filings", lambda f, e: ("30830725",)),
    ("prh_api.list_fi_dates", lambda f, e: ("2919415-2",)),
    ("sk_registeruz.fetch_entity", lambda f, e: (9000014,)),
    ("sk_registeruz.fetch_zavierka", lambda f, e: (1,)),
    ("sk_registeruz.fetch_vykaz", lambda f, e: (1,)),
    ("sk_registeruz.fetch_sablona", lambda f, e: (699,)),
])
def test_register_helper_reports_into_errors_out_param(helper, call):
    """Each helper keeps its swallow-into-[]/None contract but, when handed an
    ``errors`` list, appends one structured record naming its own stage."""
    import importlib

    mod_name, fn_name = helper.split(".")
    fn = getattr(importlib.import_module(f"company_corpus.registers.{mod_name}"), fn_name)
    kw = {"key": "k"} if fn_name == "fetch_bnb_deposit" else {}

    errors: list[dict] = []
    result = fn(*call(None, None), fetcher=_RaisingFetcher(), errors=errors, **kw)
    assert result in ([], None)
    assert len(errors) == 1
    rec = errors[0]
    assert rec["stage"] == fn_name and rec["source"]
    assert rec["entity_id"] is not None
    assert rec["error"].startswith("RuntimeError: simulated network error")
    # Without the out-parameter, today's contract is untouched.
    assert fn(*call(None, None), fetcher=_RaisingFetcher(), **kw) in ([], None)


def test_prh_iter_fi_all_reports_into_errors_out_param():
    from company_corpus.registers.prh_api import iter_fi_all

    errors: list[dict] = []
    assert list(iter_fi_all("2024-12-31", fetcher=_RaisingFetcher(), errors=errors)) == []
    assert len(errors) == 1 and errors[0]["stage"] == "iter_fi_all"
    assert errors[0]["error"].startswith("RuntimeError")


# --------------------------------------------------------------------------
# (i) the folded fetch-error message is capped; the trail gate fails loud
# --------------------------------------------------------------------------
def test_fetch_errors_message_caps_at_five():
    from company_corpus.registers._common import _fetch_errors_message

    errs = [{"stage": f"stage{i}", "error": f"boom{i}"} for i in range(8)]
    msg = _fetch_errors_message(errs)
    assert msg.startswith("stage0: boom0; ")
    assert "stage4: boom4" in msg and "stage5" not in msg
    assert msg.endswith("… (+3 more)")
    assert _fetch_errors_message(errs[:5]) == "; ".join(
        f"stage{i}: boom{i}" for i in range(5))


def test_record_out_errors_requires_the_write_flag(tmp_path):
    """Every reporting command defines --write; a namespace without it is a
    programming error, not a dry run."""
    import argparse

    with pytest.raises(AttributeError):
        cli._record_out_errors(argparse.Namespace(), Config(data_dir=tmp_path),
                               {"error_items": [{"error": "x"}]})
