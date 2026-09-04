import json
from pathlib import Path

import pytest

from company_corpus.config import Config
from company_corpus.eu.entities import Entity
from company_corpus.eu.financials import build_eu_financials, facts_for_entity
from company_corpus.xbrl import IFRS_CONCEPTS
from company_corpus.financials import summaries_from_flat

FIX = Path(__file__).parent / "fixtures" / "eu"


class FakeFetcher:
    """Routes get_json by URL: the entity filings list vs each report json_url."""
    def __init__(self, filings, reports):
        self._filings = filings        # list of filing attribute dicts
        self._reports = reports        # {json_url_path: report dict}
    def get_json(self, url, **kw):
        if "/api/entities/" in url:
            return {"data": [{"id": a["fxo_id"], "attributes": a} for a in self._filings]}
        path = url.replace("https://filings.xbrl.org", "")
        return self._reports[path]


def _report(concept_val):
    return {"facts": {"f": {"value": concept_val, "dimensions": {
        "concept": "ifrs-full:Revenue", "entity": "x", "unit": "iso4217:EUR",
        "period": "2020-01-01T00:00:00/2021-01-01T00:00:00"}}}}


def test_facts_for_entity_unions_filings():
    filings = [{"fxo_id": "1", "country": "FR", "period_end": "2020-12-31",
                "date_added": "2021-05-01 00:00:00",
                "json_url": "/r/2020.json", "package_url": "/r/2020.zip", "report_url": "/r/2020.html"}]
    fetcher = FakeFetcher(filings, {"/r/2020.json": _report(100)})
    ent = Entity(lei="LEI123", name="X", country="FR")
    flat = facts_for_entity(ent, fetcher=fetcher)
    assert "Revenue" in flat
    assert flat["Revenue"][0]["val"] == 100
    assert flat["Revenue"][0]["filed"] == "2021-05-01"


def test_build_eu_financials_writes_unified_rows(tmp_path, monkeypatch):
    filings = [{"fxo_id": "1", "country": "FR", "period_end": "2020-12-31",
                "date_added": "2021-05-01 00:00:00",
                "json_url": "/r/2020.json", "package_url": "/r/2020.zip", "report_url": "/r/2020.html"}]
    fetcher = FakeFetcher(filings, {"/r/2020.json": _report(100)})
    # resolve_entities hits GLEIF; stub it to a fixed entity for this unit test.
    monkeypatch.setattr("company_corpus.eu.financials.resolve_entities",
                        lambda specs, **kw: [Entity(lei="LEI123", name="X", country="FR")])
    cfg = Config(data_dir=tmp_path)
    rep = build_eu_financials([{"lei": "LEI123"}], fetcher=fetcher, config=cfg, write=True)
    assert rep["entities"] == 1 and rep["with_financials"] == 1 and rep["periods"] == 1
    out = (tmp_path / "financials_eu" / "LEI123.jsonl").read_text().splitlines()
    rows = [json.loads(x) for x in out]
    rev = next(r for r in rows if r["kind"] == "reported" and r["concept"] == "revenue")
    assert rev["value"] == 100 and rev["lei"] == "LEI123" and rev["currency"] == "EUR"
    # Canonical RowBase (ARCH-C1): EU issuer id is the LEI (entity_id, id_scheme
    # "lei"), country from GLEIF, source "esef", form = the ESEF doc_type.
    assert rev["entity_id"] == "LEI123" and rev["id_scheme"] == "lei"
    assert rev["country"] == "FR" and rev["source"] == "esef"
    assert rev["form"] == "annual_report" and rev["is_financial"] is None
    assert rev["publication_date"] == "2021-05-01"
    assert (tmp_path / "reports" / "eu_financials_coverage.jsonl").exists()


def test_build_eu_financials_no_lei_records_coverage(tmp_path, monkeypatch):
    # An unresolved entity (no LEI) must be recorded in coverage, never dropped.
    monkeypatch.setattr("company_corpus.eu.financials.resolve_entities",
                        lambda specs, **kw: [Entity(lei=None, name="Unknown", country="FR")])
    cfg = Config(data_dir=tmp_path)
    rep = build_eu_financials([{"name": "Unknown"}], fetcher=FakeFetcher([], {}), config=cfg)
    assert rep["entities"] == 1 and rep["no_financials"] == 1 and rep["with_financials"] == 0
    cov = [json.loads(x) for x in
           (tmp_path / "reports" / "eu_financials_coverage.jsonl").read_text().splitlines()]
    assert cov[0]["status"] == "unresolved" and cov[0]["lei"] is None


def test_build_eu_financials_no_filings_records_coverage(tmp_path, monkeypatch):
    # An entity with a LEI but no indexed filings -> no-financials, still recorded.
    monkeypatch.setattr("company_corpus.eu.financials.resolve_entities",
                        lambda specs, **kw: [Entity(lei="LEI999", name="NoFilings", country="FR")])
    cfg = Config(data_dir=tmp_path)
    rep = build_eu_financials([{"lei": "LEI999"}], fetcher=FakeFetcher([], {}), config=cfg)
    assert rep["entities"] == 1 and rep["no_financials"] == 1 and rep["with_financials"] == 0
    cov = [json.loads(x) for x in
           (tmp_path / "reports" / "eu_financials_coverage.jsonl").read_text().splitlines()]
    assert cov[0]["status"] == "no-financials" and cov[0]["lei"] == "LEI999"


def test_real_esef_fsecure_end_to_end(tmp_path, monkeypatch):
    # REAL-value guard on the whole IFRS chain (facts_for_entity ->
    # summaries_from_flat -> compute_derived -> build_eu_financials), using a genuine
    # filings.xbrl.org filing: F-Secure Oyj FY2022, LEI 9845006BFDJF0375E466 (see the
    # fixture's _provenance block: fxo_id, source URL + sha256). All figures are
    # verbatim EUR from that report; a silent mapping regression would fail here.
    report = json.loads((FIX / "fsecure_esef_2022.json").read_text())
    lei = "9845006BFDJF0375E466"
    json_path = "/9845006BFDJF0375E466/2022-12-31/ESEF/FI/1/9845006BFDJF0375E466-2022-12-31-fi.json"
    filings = [{"fxo_id": f"{lei}-2022-12-31-ESEF-FI-1", "country": "FI",
                "period_end": "2022-12-31", "date_added": "2023-02-21 10:33:44.055676",
                "json_url": json_path, "report_url": json_path.replace(".json", ".html")}]

    class _Fetcher:
        def get_json(self, url, **kw):
            if "/api/entities/" in url:
                return {"data": [{"id": a["fxo_id"], "attributes": a} for a in filings]}
            if url.endswith(json_path):
                return report
            raise RuntimeError(url)

    fetcher = _Fetcher()
    ent = Entity(lei=lei, name="F-Secure Oyj", country="FI")

    # 1) facts_for_entity: real OIM facts, EUR, carrying the filing's publication date.
    flat = facts_for_entity(ent, fetcher=fetcher)
    assert flat["Revenue"][0]["val"] == 111017000
    assert flat["Revenue"][0]["unit"] == "EUR"
    assert flat["Revenue"][0]["filed"] == "2023-02-21"

    # 2) summaries_from_flat + 3) compute_derived (sector_known=False, exactly as the
    #    EU pillar drives it). Revenue + equity reported; derived to the cent.
    s = summaries_from_flat(flat, concepts=IFRS_CONCEPTS, company="F-Secure Oyj",
                            company_current="F-Secure Oyj", sic=None, sector_known=False)
    fy = next(x for x in s if x.frequency == "annual" and x.fy == 2022)
    assert fy.values["revenue"]["value"] == 111017000
    assert fy.values["equity"]["value"] == 24804000
    assert fy.values["equity"]["tag"] == "EquityAttributableToOwnersOfParent"
    d = fy.derived
    assert d["working_capital"]["value"] == 20577000            # 47,828,000 - 27,251,000, exact
    assert d["operating_margin"]["value"] == pytest.approx(38770000 / 111017000 * 100)
    assert d["roe"]["value"] == pytest.approx(30153000 / 24804000 * 100)  # no NCI -> not gated (E-I3)
    assert d["current_ratio"]["sector_relevant"] is None        # sector unknown -> not asserted (E-I4)

    # 4) build_eu_financials end-to-end -> unified rows on disk.
    monkeypatch.setattr("company_corpus.eu.financials.resolve_entities",
                        lambda specs, **kw: [ent])
    cfg = Config(data_dir=tmp_path)
    rep = build_eu_financials([{"lei": lei}], fetcher=fetcher, config=cfg, write=True)
    assert rep["with_financials"] == 1 and rep["periods"] >= 1
    rows = [json.loads(x) for x in
            (tmp_path / "financials_eu" / f"{lei}.jsonl").read_text().splitlines()]
    rev = next(r for r in rows if r["kind"] == "reported" and r["concept"] == "revenue")
    eqr = next(r for r in rows if r["kind"] == "reported" and r["concept"] == "equity")
    wc = next(r for r in rows if r["kind"] == "derived" and r["concept"] == "working_capital")
    assert rev["value"] == 111017000 and rev["currency"] == "EUR" and rev["is_financial"] is None
    assert eqr["value"] == 24804000
    assert wc["value"] == 20577000                              # derived monetary, to the cent


def test_build_eu_financials_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("company_corpus.eu.financials.resolve_entities",
                        lambda specs, **kw: [Entity(lei="LEI123", name="X", country="FR")])
    filings = [{"fxo_id": "1", "country": "FR", "period_end": "2020-12-31",
                "date_added": "2021-05-01 00:00:00",
                "json_url": "/r/2020.json", "package_url": "/r/2020.zip", "report_url": "/r/2020.html"}]
    fetcher = FakeFetcher(filings, {"/r/2020.json": _report(100)})
    cfg = Config(data_dir=tmp_path)
    rep = build_eu_financials([{"lei": "LEI123"}], fetcher=fetcher, config=cfg, write=False)
    assert rep["with_financials"] == 1 and rep["coverage_path"] is None
    assert not (tmp_path / "financials_eu").exists()
    assert not (tmp_path / "reports").exists()
