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
            pass

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
