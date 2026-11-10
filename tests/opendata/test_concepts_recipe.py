"""The Recipe over a real store gives what F1 gives, on real filings (spec D4)."""
import json
import os
from datetime import datetime, timezone

import pytest

REQUIRED = os.environ.get("REQUIRE_DUCKLAKE") == "1"
if not REQUIRED:
    pytest.importorskip("myopendata")
import duckdb  # noqa: E402

try:
    duckdb.connect().execute("INSTALL ducklake; LOAD ducklake")
except duckdb.Error as exc:
    if REQUIRED:
        raise
    pytest.skip(f"ducklake extension unavailable: {exc}", allow_module_level=True)

from myopendata.config import StoreConfig  # noqa: E402
from myopendata.contracts.standard import (  # noqa: E402
    ENTITY_OBSERVATIONS,
    FILING_STATEMENTS,
    FILINGS,
    FUNDAMENTALS,
)
from myopendata.publisher import approve_contract, publish_pending  # noqa: E402
from myopendata.reader import Store  # noqa: E402

from company_corpus.concepts.selection import RECIPE_NAME, recipe  # noqa: E402
from company_corpus.concepts.summaries import select_for_f1  # noqa: E402
from company_corpus.config import normalize_cik  # noqa: E402
from company_corpus.opendata.batches import store_batches  # noqa: E402
from company_corpus.opendata.statements import statements_path  # noqa: E402
from tests.concepts.support import fixture  # noqa: E402
from tests.opendata.factories import submissions_main, write_archive, write_companyfacts  # noqa: E402

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
CASES = ["marriott", "metlife", "pepsico", "federal_realty", "otis", "equinix", "jpmorgan"]


def _accepted(day: str) -> str:
    return f"{day}T21:00:00.000Z"


def _load(config, name):
    fx = fixture(name)
    cik = normalize_cik(fx["cik"])
    write_companyfacts(config, cik, fx["facts"])
    filings = {}
    for ns in fx["facts"]["facts"].values():
        for body in ns.values():
            for points in body["units"].values():
                for p in points:
                    filings[p["accn"]] = (p["accn"], _accepted(p["filed"]), p["form"])
    write_archive(config, cik, submissions_main(cik=cik, filings=sorted(filings.values())))
    path = statements_path(config, cik)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in fx["statements"]), encoding="utf-8")
    return cik, fx


@pytest.fixture
def published(config, tmp_path):
    store = StoreConfig(data_root=tmp_path / "store", catalog=str(tmp_path / "meta.ducklake"))
    for contract in (ENTITY_OBSERVATIONS, FUNDAMENTALS, FILING_STATEMENTS, FILINGS):
        approve_contract(store, contract, "test")
    loaded = {name: _load(config, name) for name in CASES}
    rep = store_batches([c for c, _ in loaded.values()], config=config, landing=store.landing,
                        write=True, offline=True, run_id="r", now=NOW)
    assert [o.error for o in rep.failed] == []
    out = publish_pending(store, now=NOW)
    assert out.exit_code == 0 and not out.quarantined, out.to_json()
    return store, loaded


def test_recipe_equals_f1_on_real_filings(published):
    store, loaded = published
    with Store.open(store) as s:
        t = s.assemble(recipe())
    assert t.schema.metadata[b"myopendata.recipe"].startswith(RECIPE_NAME.encode() + b"@2.1.0")
    by_issuer = {}
    for r in t.to_pylist():
        by_issuer.setdefault(r["entity_value"], {})[(r["concept"], r["valid_date"], r["frequency"])] = r
    assert set(by_issuer) == {c for c, _ in loaded.values()}
    for name, (cik, fx) in loaded.items():
        f1 = {(r["concept"], r["valid_date"], r["frequency"]): r
              for r in select_for_f1(fx["facts"], fx["statements"]).rows}
        common = set(f1) & set(by_issuer[cik])
        assert len(common) > 30, name
        differ = [k for k in common if (f1[k]["value"], f1[k]["original_tag"], f1[k]["location"], f1[k]["conflict"])
                  != (by_issuer[cik][k]["value"], by_issuer[cik][k]["original_tag"],
                      by_issuer[cik][k]["location"], by_issuer[cik][k]["conflict"])]
        assert differ == [], name


def test_recipe_reads_as_of(published):
    store, loaded = published
    cik, _ = loaded["marriott"]
    with Store.open(store) as s:
        early = s.assemble(recipe(), datetime(2026, 2, 1, tzinfo=timezone.utc)).to_pylist()
        late = s.assemble(recipe()).to_pylist()
    assert not [r for r in early if r["entity_value"] == cik]     # its filings came later
    assert [r["location"] for r in late if r["entity_value"] == cik and r["concept"] == "long_term_debt"
            and r["valid_date"].isoformat() == "2025-12-31"] == ["statement"]


def test_real_positions_upgrade_inferred_values_without_touching_a_fact(config, tmp_path):
    store = StoreConfig(data_root=tmp_path / "store", catalog=str(tmp_path / "meta.ducklake"))
    for contract in (ENTITY_OBSERVATIONS, FUNDAMENTALS, FILING_STATEMENTS, FILINGS):
        approve_contract(store, contract, "test")
    cik, fx = _load(config, "marriott")
    newest = max(s["accn"] for s in fx["statements"])
    path = statements_path(config, cik)
    path.write_text("".join(json.dumps(s) + "\n" for s in fx["statements"] if s["accn"] != newest),
                    encoding="utf-8")

    def run(run_id):
        rep = store_batches([cik], config=config, landing=store.landing, write=True, offline=True,
                            run_id=run_id, now=NOW)
        assert rep.issuers[0].status == "ok"
        return publish_pending(store, now=NOW)

    def debt():
        with Store.open(store) as s:
            rows = [r for r in s.assemble(recipe()).to_pylist()
                    if r["concept"] == "long_term_debt" and r["filing_ref"] == newest]
        latest = max(rows, key=lambda r: r["valid_date"])
        return latest["value"], latest["location"]

    assert run("r1").exit_code == 0
    value, location = debt()
    assert location == "inferred"
    path.write_text("".join(json.dumps(s) + "\n" for s in fx["statements"]), encoding="utf-8")
    second = run("r2")
    assert second.exit_code == 0 and not second.quarantined, second.to_json()
    by_table = {p["contract"]: p for p in second.published}
    assert "fundamentals_v3" not in by_table and by_table["filing_statements_v1"]["rows"] > 0
    assert debt() == (value, "statement")


def test_replay_writes_nothing_new(published, config):
    store, loaded = published
    store_batches([c for c, _ in loaded.values()], config=config, landing=store.landing,
                  write=True, offline=True, run_id="r2", now=NOW)
    replay = publish_pending(store, now=NOW)
    assert replay.exit_code == 0 and not replay.published and not replay.quarantined
