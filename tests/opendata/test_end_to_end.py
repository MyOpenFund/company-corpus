"""Producer -> landing -> publisher -> reader on a DuckLake file catalogue (spec §7).

Needs the DuckDB ``ducklake`` extension: DuckDB downloads it with its own HTTP client on first
use (network required; CI pre-installs it). The module skips when it cannot be loaded, unless
``REQUIRE_DUCKLAKE=1`` (the CI ``store`` job): then that is a failure, so CI never passes
without having run this test.
"""
import importlib
import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest

REQUIRED = os.environ.get("REQUIRE_DUCKLAKE") == "1"


def _need(module: str):
    return importlib.import_module(module) if REQUIRED else pytest.importorskip(module)


_need("myopendata")
duckdb = _need("duckdb")

try:
    duckdb.connect().execute("INSTALL ducklake; LOAD ducklake")
except duckdb.Error as exc:
    if REQUIRED:
        raise
    pytest.skip(f"DuckDB ducklake extension unavailable (needs network on first use): {exc}",
                allow_module_level=True)

from myopendata.config import StoreConfig  # noqa: E402
from myopendata.contracts.standard import ENTITY_OBSERVATIONS, FUNDAMENTALS  # noqa: E402
from myopendata.publisher import approve_contract, publish_pending  # noqa: E402
from myopendata.reader import Store  # noqa: E402

from company_corpus.opendata.batches import store_batches  # noqa: E402
from tests.opendata.factories import (  # noqa: E402
    companyfacts, point, submissions_main, write_archive, write_companyfacts)

CIK = "0000000042"
K, KA = "0000000042-24-000001", "0000000042-24-000099"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def test_publish_replay_and_restatement(config, tmp_path):
    write_companyfacts(config, CIK, companyfacts({"Revenues": {"USD": [
        point(100, "2024-06-30", start="2023-07-01", accn=K),
        point(110, "2024-06-30", start="2023-07-01", accn=KA, form="10-K/A",
              filed="2024-10-15")]}}))
    write_archive(config, CIK, submissions_main(cik=CIK, filings=[
        (K, "2024-07-30T20:06:22.000Z", "10-K"), (KA, "2024-10-15T21:00:00.000Z", "10-K/A")]))
    store = StoreConfig(data_root=tmp_path / "store", catalog=str(tmp_path / "meta.ducklake"))
    approve_contract(store, ENTITY_OBSERVATIONS, "test")
    approve_contract(store, FUNDAMENTALS, "test")

    def run():
        rep = store_batches([CIK], config=config, landing=store.landing, write=True,
                            run_id="r", now=NOW)
        assert rep.issuers[0].status == "ok"
        return publish_pending(store, now=NOW)

    first = run()
    assert first.exit_code == 0 and sum(p["rows"] for p in first.published) == 4, first.to_json()
    replay = run()
    assert replay.exit_code == 0 and not replay.published and len(replay.skipped_duplicates) == 2
    with Store.open(store) as s:
        def revenue(at):
            t = s.read_asof("fundamentals", at, filters={"concept": "revenue"})
            return [r["value"] for r in t.to_pylist()]

        assert revenue(datetime(2024, 9, 1, tzinfo=timezone.utc)) == [Decimal(100)]
        assert revenue(datetime(2024, 12, 1, tzinfo=timezone.utc)) == [Decimal(110)]
        assert s.read_asof("fundamentals").num_rows == 1
