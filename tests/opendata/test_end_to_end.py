"""Producer -> landing -> publisher -> reader on a DuckLake file catalogue (spec §7).

Needs the DuckDB ``ducklake`` extension: DuckDB downloads it with its own HTTP client on first
use (network required; CI pre-installs it). The module skips when it cannot be loaded, unless
``REQUIRE_DUCKLAKE=1`` (the CI ``store`` job): then that is a failure, so CI never passes
without having run this test.
"""
import importlib
import json
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
from myopendata.contracts.standard import ENTITY_OBSERVATIONS, FILINGS, FUNDAMENTALS  # noqa: E402
from myopendata.publisher import approve_contract, publish_pending  # noqa: E402
from myopendata.reader import Store  # noqa: E402

from company_corpus.opendata.batches import store_batches  # noqa: E402
from tests.opendata.factories import (  # noqa: E402
    companyfacts, point, submissions_main, write_archive, write_companyfacts)
from tests.conftest import FakeFetcher  # noqa: E402

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
    approve_contract(store, FILINGS, "test")

    def run():
        rep = store_batches([CIK], config=config, landing=store.landing, write=True,
                            run_id="r", now=NOW)
        assert rep.issuers[0].status == "ok"
        return publish_pending(store, now=NOW)

    first = run()
    assert first.exit_code == 0 and sum(p["rows"] for p in first.published) == 6, first.to_json()
    replay = run()
    assert replay.exit_code == 0 and not replay.published and len(replay.skipped_duplicates) == 3
    with Store.open(store) as s:
        def revenue(at):
            t = s.read_asof("fundamentals", at, filters={"original_tag": "us-gaap:Revenues"})
            return [r["value"] for r in t.to_pylist()]

        assert revenue(datetime(2024, 9, 1, tzinfo=timezone.utc)) == [Decimal(100)]
        assert revenue(datetime(2024, 12, 1, tzinfo=timezone.utc)) == [Decimal(110)]
        assert s.read_asof("fundamentals").num_rows == 1
    with Store.open(store) as s:
        forms = {r["filing_ref"]: r["form"] for r in s.read_asof("filings").to_pylist()}
    assert forms == {K: "10-K", KA: "10-K/A"}


def _store(tmp_path):
    store = StoreConfig(data_root=tmp_path / "store", catalog=str(tmp_path / "meta.ducklake"))
    approve_contract(store, ENTITY_OBSERVATIONS, "test")
    approve_contract(store, FUNDAMENTALS, "test")
    approve_contract(store, FILINGS, "test")
    return store


def _publish(store, config, now):
    rep = store_batches([CIK], config=config, landing=store.landing, write=True, run_id="r",
                        now=now, fetcher=FakeFetcher({MAIN: json.dumps(NEW_MAIN)}, config=config))
    assert rep.issuers[0].status == "ok", rep.issuers[0].error
    return publish_pending(store, now=now)


MAIN = f"submissions/CIK{CIK}.json"
NEW = "0000000042-26-000100"
NEW_MAIN = submissions_main(cik=CIK, filings=[
    (K, "2024-07-30T20:06:22.000Z", "10-K"), (NEW, "2026-11-02T21:30:00.000Z", "10-Q")])


def test_a_stale_refresh_publishes_only_the_new_row(config, tmp_path):
    write_companyfacts(config, CIK, companyfacts({"Revenues": {"USD": [
        point(100, "2024-06-30", start="2023-07-01", accn=K)]}}))
    write_archive(config, CIK, submissions_main(cik=CIK, filings=[
        (K, "2024-07-30T20:06:22.000Z", "10-K")]))
    store = _store(tmp_path)
    first = _publish(store, config, NOW)
    assert first.exit_code == 0 and not first.quarantined

    # A later filing arrives in companyfacts; the archive predates it and refreshes itself.
    write_companyfacts(config, CIK, companyfacts({"Revenues": {"USD": [
        point(100, "2024-06-30", start="2023-07-01", accn=K),
        point(30, "2026-09-30", start="2026-07-01", accn=NEW, form="10-Q", filed="2026-11-02")]}}))
    later = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    second = _publish(store, config, later)

    assert second.exit_code == 0 and not second.quarantined, second.to_json()
    by_table = {p["contract"]: p for p in second.published}
    assert by_table["fundamentals_v3"]["rows"] == 1
    assert by_table["fundamentals_v3"]["rows_skipped"] == 1
    with Store.open(store) as s:
        rows = s.read_asof("fundamentals").to_pylist()
    assert sorted(r["value"] for r in rows) == [Decimal(30), Decimal(100)]
    new = next(r for r in rows if r["value"] == Decimal(30))
    assert new["known_at"] == datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc)


def test_known_limit_a_lower_accession_arriving_second_is_quarantined(config, tmp_path):
    """Documents a limit (README, "Known limit"): two filings accepted in the same second with
    an equal value; the lower accession appears only in a later companyfacts. source_doc_id
    then changes for that key, and the batch is quarantined until an operator repairs it."""
    high, low = "0000000042-24-000050", "0000000042-24-000020"
    accepted = "2024-07-30T20:06:22.000Z"
    write_archive(config, CIK, submissions_main(cik=CIK, filings=[
        (high, accepted, "10-K"), (low, accepted, "10-K")]))
    write_companyfacts(config, CIK, companyfacts({"Revenues": {"USD": [
        point(100, "2024-06-30", start="2023-07-01", accn=high)]}}))
    store = _store(tmp_path)
    first = _publish(store, config, NOW)
    assert first.exit_code == 0 and not first.quarantined

    write_companyfacts(config, CIK, companyfacts({"Revenues": {"USD": [
        point(100, "2024-06-30", start="2023-07-01", accn=high),
        point(100, "2024-06-30", start="2023-07-01", accn=low)]}}))
    second = _publish(store, config, NOW)

    assert second.exit_code != 0 and len(second.quarantined) == 1, second.to_json()
    assert "conflicting_republish" in json.dumps(second.quarantined)
