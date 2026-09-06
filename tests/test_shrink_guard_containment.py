"""A refused write is one entity's failure, never the run's.

``ShrinkGuardError`` is raised by ``Storage._write_table`` when ``--replace``
would drop more of a table than ``no_shrink_fraction`` allows. Exactly one
caller caught it -- ``pipeline.run_financials``, family F1 -- so on every other
pillar the first refused write aborted the whole process: the register producer
lost the entities it had not reached, the ownership run lost its remaining
issuers, and all three coverage writers (register, EU financials, EU acquire)
took the run down at the very end, after all the work, and wrote nothing.

That is the exact doctrine ARCHITECTURE.md states one issuer's failure must
never have. These tests hold every site to it: the run finishes, the refusal is
visible as a coverage row and an error item, and nothing else is lost.

The guard is tripped with real writes, not a patched raiser: a table that
already holds two record groups, ``replace_tables=True``, and a run producing
one group.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from company_corpus.config import Config
from company_corpus.eu import acquire as acq
from company_corpus.eu.documents import Document
from company_corpus.eu.entities import Entity
from company_corpus.models import FilingRecord
from company_corpus.pipeline import process_ownership
from company_corpus.registers._common import _emit_entity_rows, _finalise_coverage, _make_out
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType

from .test_ownership import FORM4_SUBMISSION

LEI = "5493001KJTIIGC8Y1R12"


@pytest.fixture
def replacing_config(tmp_path) -> Config:
    """``--replace`` with the default zero-tolerance no-shrink guard."""
    return Config(data_dir=tmp_path / "data", contact="t@e.com",
                  replace_tables=True, no_shrink_fraction=0.0)


def _seed_table(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _fin_row(period_end: str, **kw) -> dict:
    row = {"entity_id": "999", "source": "brreg", "period_end": period_end,
           "frequency": "annual", "basis": None, "kind": "reported",
           "concept": "assets", "value": 1, "unit": "NOK"}
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# (a) register producers: the per-entity table write
# ---------------------------------------------------------------------------
def test_register_table_refusal_is_one_entity_not_the_batch(replacing_config):
    storage = Storage(replacing_config)
    _seed_table(replacing_config.financials_register_dir / "999.jsonl",
                [_fin_row("2022-12-31"), _fin_row("2023-12-31")])
    out, coverage = _make_out(), []

    _emit_entity_rows("999", [_fin_row("2023-12-31")], 1, {"orgnr": "999"},
                      storage, out, coverage, write=True)

    assert coverage[0]["status"] == "source-error"
    assert "refusing to drop" in coverage[0]["error"]
    assert out["errors"] == 1 and out["paths"] == []
    assert out["error_items"] and out["error_items"][0]["entity_id"] == "999"


# ---------------------------------------------------------------------------
# (b) the coverage finalisers
# ---------------------------------------------------------------------------
def test_register_coverage_refusal_does_not_abort(replacing_config):
    _seed_table(replacing_config.reports_dir / "register_coverage_brreg.jsonl",
                [{"orgnr": "1", "status": "ok"}, {"orgnr": "2", "status": "ok"}])
    out = _make_out()

    result = _finalise_coverage(out, [{"orgnr": "1", "status": "ok"}],
                                replacing_config, "brreg", write=True)

    assert result["coverage_path"] is None, "nothing was written, so nothing is claimed"
    assert result["errors"] == 1
    assert "refusing to drop" in result["error_items"][0]["error"]


def test_eu_financials_table_refusal_is_one_issuer_and_tagged_local(
        replacing_config, monkeypatch):
    """The per-issuer ESEF table write, refused: the issuer is recorded
    ``source-error`` and the run goes on -- and the error item is tagged
    ``storage``, the LOCAL refusal tag, not ``esef``. The aggregator answered
    perfectly here; it was the corpus that refused its own write, and the trail
    (and the run report behind it) must not read as "filings.xbrl.org failed".
    """
    from company_corpus.eu import financials as fin

    from .test_eu_financials import FakeFetcher, _report

    _seed_table(replacing_config.financials_eu_dir / f"{LEI}.jsonl",
                [{"source": "esef", "period_end": "2019-12-31", "frequency": "annual",
                  "basis": None, "concept": "revenue", "value": 1},
                 {"source": "esef", "period_end": "2018-12-31", "frequency": "annual",
                  "basis": None, "concept": "revenue", "value": 2}])
    monkeypatch.setattr(fin, "resolve_entities",
                        lambda specs, **kw: [Entity(lei=LEI, name="X", country="FR")])
    filings = [{"fxo_id": "1", "country": "FR", "period_end": "2020-12-31",
                "date_added": "2021-05-01 00:00:00", "json_url": "/r/2020.json",
                "package_url": "/r/2020.zip", "report_url": "/r/2020.html"}]

    out = fin.build_eu_financials(
        [{"lei": LEI}], fetcher=FakeFetcher(filings, {"/r/2020.json": _report(100)}),
        config=replacing_config, write=True)

    assert out["entities"] == 1 and out["with_financials"] == 0
    assert out["paths"] == [], "nothing landed for this issuer"
    item = next(i for i in out["error_items"] if "refusing to drop" in str(i["error"]))
    assert item["source"] == "storage" and item["entity_id"] == LEI
    cov = [c for c in _read_coverage(replacing_config) if c["lei"] == LEI]
    assert cov and cov[0]["status"] == "source-error"


def _read_coverage(config) -> list[dict]:
    path = config.reports_dir / "eu_financials_coverage.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if x]


def test_eu_financials_coverage_refusal_is_tagged_local(replacing_config, monkeypatch):
    """Same for the run's last write: the refused coverage file is the corpus's
    own refusal, tagged ``storage`` like every register's."""
    from company_corpus.eu import financials as fin

    _seed_table(replacing_config.reports_dir / "eu_financials_coverage.jsonl",
                [{"lei": LEI, "status": "ok"}, {"lei": "OTHER", "status": "ok"}])
    monkeypatch.setattr(fin, "resolve_entities",
                        lambda specs, **kw: [Entity(lei=LEI, name="X", country="FI")])

    class _Dead:
        def get_json(self, url, **_):
            raise RuntimeError("aggregator down")

    out = fin.build_eu_financials([{"lei": LEI}], fetcher=_Dead(),
                                  config=replacing_config, write=True)

    refusals = [i for i in out["error_items"] if "refusing to drop" in str(i["error"])]
    assert refusals and all(i["source"] == "storage" for i in refusals)
    dead = [i for i in out["error_items"] if "aggregator down" in str(i["error"])]
    assert dead and all(i["source"] == "esef" for i in dead), (
        "a dead aggregator is still the aggregator's failure")


def test_eu_financials_coverage_refusal_does_not_abort(replacing_config, monkeypatch):
    from company_corpus.eu import financials as fin

    _seed_table(replacing_config.reports_dir / "eu_financials_coverage.jsonl",
                [{"lei": LEI, "status": "ok"}, {"lei": "OTHER", "status": "ok"}])
    monkeypatch.setattr(fin, "resolve_entities",
                        lambda specs, **kw: [Entity(lei=LEI, name="X", country="FI")])

    class _Dead:
        def get_json(self, url, **_):
            raise RuntimeError("aggregator down")

    out = fin.build_eu_financials([{"lei": LEI}], fetcher=_Dead(),
                                  config=replacing_config, write=True)

    assert out["coverage_path"] is None
    assert any("refusing to drop" in str(i.get("error")) for i in out["error_items"])


def test_eu_acquire_coverage_refusal_does_not_abort(replacing_config, monkeypatch):
    _seed_table(replacing_config.reports_dir / "eu_coverage.jsonl",
                [{"lei": LEI, "gap": None}, {"lei": "OTHER", "gap": None}])
    monkeypatch.setattr(acq, "resolve_entities",
                        lambda specs, *, fetcher: [Entity(LEI, "X", "DE", resolution="lei")])

    class _Backend:
        def __init__(self, *a, **k):
            self.errors = []

        def discover(self, e):
            return [Document("de-1", LEI, "DE", "annual_report", date(2023, 12, 31),
                             None, "x", "de", "oam-de", [{"name": "r", "sha256": "h"}], {})]

    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"DE": _Backend})
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _Backend)

    out = acq.acquire([{"lei": LEI}], fetcher=object(), config=replacing_config,
                      download=False, write=True)

    assert out["documents"] == 1, "the discovery this run did is still reported"
    assert out["coverage_path"] is None
    assert any("refusing to drop" in str(e.get("error")) for e in out["errors"])


# ---------------------------------------------------------------------------
# (c) ownership
# ---------------------------------------------------------------------------
def test_ownership_table_refusal_is_one_issuer_not_the_run(replacing_config,
                                                           make_fetcher):
    st = Storage(replacing_config)
    _seed_table(replacing_config.ownership_dir / "0000320193.jsonl",
                [{"cik": "0000320193", "accession": "old-1"},
                 {"cik": "0000320193", "accession": "old-2"}])
    rec = FilingRecord(cik="320193", form_type=FormType.E1, sec_form="4",
                       accession="acc-f4", company="Apple Inc.",
                       filing_date=date(2024, 5, 1),
                       primary_doc_url="https://x/form4.xml",
                       submission_url="https://sec/form4sub.txt")
    st.save_records([rec], dry_run=False)
    fetcher = make_fetcher({"form4sub.txt": FORM4_SUBMISSION})
    fetcher.config = replacing_config

    rep = process_ownership(["320193"], dry_run=False, config=replacing_config,
                            fetcher=fetcher, storage=st)

    assert rep.errors == 1
    assert "refusing to drop" in rep.error_items[0]["error"]
    assert rep.parsed_insider == 1, "the filing was still downloaded and parsed"
