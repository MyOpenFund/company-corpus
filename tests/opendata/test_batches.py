import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("myopendata")
pq = pytest.importorskip("pyarrow.parquet")

from company_corpus.opendata.batches import store_batches  # noqa: E402
from company_corpus.opendata.submissions import archive_dir, load_archive  # noqa: E402
from tests.conftest import FakeFetcher  # noqa: E402
from tests.opendata.factories import (  # noqa: E402
    companyfacts, point, submissions_main, write_archive, write_companyfacts)

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
A, B = "0000000042", "0000000043"
K = "0000000042-24-000001"


def _issuer(config, cik, *, archive=True):
    write_companyfacts(config, cik, companyfacts({
        "Revenues": {"USD": [point(100, "2024-06-30", start="2023-07-01", accn=K)]},
        "LongTermDebt": {"USD": [point(50, "2024-06-30", accn=K)]}}))
    if archive:
        write_archive(config, cik, submissions_main(
            cik=cik, filings=[(K, "2024-07-30T20:06:22.000Z", "10-K")]))


def _run(config, landing, **kw):
    kw.setdefault("write", True)
    return store_batches([A, B], config=config, landing=landing, run_id="r1", now=NOW, **kw)


def test_one_identity_and_one_fundamentals_batch_per_issuer(config, tmp_path):
    _issuer(config, A), _issuer(config, B)
    rep = _run(config, tmp_path / "landing")
    assert [o.status for o in rep.issuers] == ["ok", "ok"]
    assert rep.issuers[0].rows == {
        "entity_observations_v1": 2, "fundamentals_v3": 2, "filing_statements_v1": 0}
    batches = sorted(p.parent.parent.name for p in (tmp_path / "landing").glob("*/*/batch.json"))
    assert batches == ["entity_observations_v1"] * 2 + ["fundamentals_v3"] * 2
    batch_id = rep.issuers[1].batches["fundamentals_v3"]
    m = json.loads((tmp_path / "landing" / "fundamentals_v3" / batch_id / "batch.json")
                   .read_text())
    assert m["producer"] == "company-corpus"
    assert all(s.startswith(f"raw/{B}/") for s in m["source_inputs"])
    assert any(s.startswith(f"raw/{B}/F1/companyfacts.json@sha256:") for s in m["source_inputs"])


def test_an_unexpected_error_in_one_issuer_never_stops_the_others(config, tmp_path, monkeypatch):
    from company_corpus.opendata import batches
    _issuer(config, A), _issuer(config, B)
    real = batches.fundamentals_rows

    def poisoned(cik, *a, **kw):
        if cik == A:
            raise ZeroDivisionError("boom")
        return real(cik, *a, **kw)

    monkeypatch.setattr(batches, "fundamentals_rows", poisoned)
    rep = _run(config, tmp_path / "landing")
    assert [o.status for o in rep.issuers] == ["failed", "ok"]
    assert rep.issuers[0].error == "unexpected: ZeroDivisionError: boom"
    assert rep.issuers[1].batches and rep.to_details()["store_batches"]["totals"]["failed"] == 1


def test_replay_gives_identical_batch_ids(config, tmp_path):
    _issuer(config, A), _issuer(config, B)
    first = _run(config, tmp_path / "landing")
    again = _run(config, tmp_path / "landing2")
    assert [o.batches for o in first.issuers] == [o.batches for o in again.issuers]


def test_dry_run_writes_nothing_and_fetches_nothing(config, tmp_path):
    _issuer(config, A), _issuer(config, B, archive=False)
    before = sorted(p for p in config.data_dir.rglob("*"))
    rep = _run(config, tmp_path / "landing", write=False)
    assert [o.status for o in rep.issuers] == ["ok", "pending_fetch"]
    assert rep.issuers[0].batches and not (tmp_path / "landing").exists()
    assert sorted(p for p in config.data_dir.rglob("*")) == before


def test_an_issuer_without_companyfacts_fails_alone(config, tmp_path):
    _issuer(config, B)
    rep = _run(config, tmp_path / "landing")
    assert [(o.cik, o.status) for o in rep.issuers] == [(A, "failed"), (B, "ok")]
    assert rep.issuers[0].error == "companyfacts_missing"


def test_missing_archive_is_fetched_with_write(config, tmp_path):
    _issuer(config, A, archive=False)
    main = submissions_main(cik=A, filings=[(K, "2024-07-30T20:06:22.000Z", "10-K")])
    fetcher = FakeFetcher({"submissions/CIK0000000042.json": json.dumps(main)}, config=config)
    rep = store_batches([A], config=config, landing=tmp_path / "l", write=True, run_id="r",
                        now=NOW, fetcher=fetcher)
    assert rep.issuers[0].status == "ok" and fetcher.calls


def test_offline_with_missing_archive_fails_the_issuer(config, tmp_path):
    _issuer(config, A, archive=False)
    rep = store_batches([A], config=config, landing=tmp_path / "l", write=True, offline=True,
                        run_id="r", now=NOW)
    assert rep.issuers[0].status == "failed"
    assert rep.issuers[0].error.startswith("submissions_archive_missing")


def test_fetch_failure_fails_the_issuer(config, tmp_path):
    _issuer(config, A, archive=False)
    rep = store_batches([A], config=config, landing=tmp_path / "l", write=True, run_id="r",
                        now=NOW, fetcher=FakeFetcher({}, config=config))
    assert rep.issuers[0].error.startswith("submissions_fetch_failed")


def test_refresh_refetches_and_changes_only_identity(config, tmp_path):
    _issuer(config, A)
    first = store_batches([A], config=config, landing=tmp_path / "l1", write=True,
                          run_id="r", now=NOW)
    main = submissions_main(cik=A, filings=[(K, "2024-07-30T20:06:22.000Z", "10-K")])
    fetcher = FakeFetcher({"submissions/CIK0000000042.json": json.dumps(main)}, config=config)
    later = datetime(2026, 12, 1, tzinfo=timezone.utc)
    again = store_batches([A], config=config, landing=tmp_path / "l2", write=True, refresh=True,
                          run_id="r", now=later, fetcher=fetcher)
    b1, b2 = first.issuers[0].batches, again.issuers[0].batches
    assert b1["fundamentals_v3"] == b2["fundamentals_v3"]
    assert b1["entity_observations_v1"] != b2["entity_observations_v1"]


def test_a_contract_breach_is_a_failed_issuer_logged_in_full(config, tmp_path, monkeypatch,
                                                            caplog):
    import company_corpus.opendata.batches as batches

    _issuer(config, A)
    real = batches.fundamentals_rows

    def broken(*a, **kw):
        res = real(*a, **kw)
        res.rows[0]["frequency"] = "ttm"
        return res

    monkeypatch.setattr(batches, "fundamentals_rows", broken)
    rep = store_batches([A], config=config, landing=tmp_path / "l", write=True, run_id="r",
                        now=NOW)
    assert rep.issuers[0].status == "failed"
    assert "value_not_allowed:frequency" in rep.issuers[0].error
    assert "value_not_allowed:frequency" in caplog.text


def test_report_totals_and_details(config, tmp_path):
    _issuer(config, A)
    rep = _run(config, tmp_path / "landing")
    t = rep.totals()
    assert t["issuers"] == 2 and t["ok"] == 1 and t["failed"] == 1
    assert t["rows"] == {
        "entity_observations_v1": 2, "fundamentals_v3": 2, "filing_statements_v1": 0}
    d = rep.to_details()["store_batches"]
    assert d["write"] is True and d["issuers"][B]["error"] == "companyfacts_missing"


# ---- a stale archive refreshes itself ----

NEW = "0000000042-26-000100"
AFTER_FETCH = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)  # the archive: 2026-11-01 09:00Z
MAIN = f"submissions/CIK{A}.json"


def _main_with_new_filing():
    return json.dumps(submissions_main(cik=A, filings=[
        (K, "2024-07-30T20:06:22.000Z", "10-K"), (NEW, "2026-11-02T21:30:00.000Z", "10-Q")]))


def _issuer_with_new_filing(config, *, filed="2026-11-02", tag="Revenues"):
    """Companyfacts cites NEW, filed after the archive was fetched and absent from it."""
    write_companyfacts(config, A, companyfacts({
        "LongTermDebt": {"USD": [point(50, "2024-06-30", accn=K)]},
        tag: {"USD": [point(30, "2026-09-30", start="2026-07-01", accn=NEW, form="10-Q",
                            filed=filed)]}}))
    write_archive(config, A, submissions_main(
        cik=A, filings=[(K, "2024-07-30T20:06:22.000Z", "10-K")]))


def _run_a(config, landing, fetcher, *, write=True, offline=False, now=AFTER_FETCH):
    rep = store_batches([A], config=config, landing=landing, write=write, offline=offline,
                        run_id="r", now=now, fetcher=fetcher)
    return rep.issuers[0]


def _known_ats(landing, table, column="known_at"):
    rows = pq.read_table(next((landing / table).rglob("*.parquet"))).to_pylist()
    return {r["id_value"] if "id_value" in r else r["original_tag"]: r[column] for r in rows}


def test_a_filing_newer_than_the_archive_refreshes_it_once(config, tmp_path):
    _issuer_with_new_filing(config)
    fetcher = FakeFetcher({MAIN: _main_with_new_filing()}, config=config)
    out = _run_a(config, tmp_path / "l", fetcher)
    assert out.status == "ok" and len(fetcher.calls) == 1
    assert out.counters["submissions_refreshed:stale"] == 1
    assert out.counters["known_at_fallback"] == 0
    assert _known_ats(tmp_path / "l", "fundamentals_v3")["us-gaap:Revenues"] == datetime(
        2026, 11, 2, 21, 30, tzinfo=timezone.utc)
    # The refresh is a new identity observation.
    assert _known_ats(tmp_path / "l", "entity_observations_v1")[A] == AFTER_FETCH


def test_the_run_after_a_refresh_fetches_nothing(config, tmp_path):
    _issuer_with_new_filing(config)
    fetcher = FakeFetcher({MAIN: _main_with_new_filing()}, config=config)
    first = _run_a(config, tmp_path / "l1", fetcher)
    again = _run_a(config, tmp_path / "l2", fetcher)
    assert len(fetcher.calls) == 1
    assert again.counters["submissions_refreshed:stale"] == 0
    assert first.batches == again.batches


def test_an_old_accession_missing_from_the_archive_never_refreshes(config, tmp_path):
    # Filed the day before the fetch: the archive will never hold it, or it would already.
    _issuer_with_new_filing(config, filed="2026-10-31")
    fetcher = FakeFetcher({}, config=config)
    out = _run_a(config, tmp_path / "l", fetcher)
    assert out.status == "ok" and fetcher.calls == []
    assert out.counters["known_at_fallback"] == 1
    assert not out.counters["submissions_refreshed:stale"] and not out.counters["submissions_stale"]


@pytest.mark.parametrize("filed, stale", [("2026-11-01", True), ("2026-10-31", False)])
def test_the_fetch_day_is_counted_in_the_edgar_day_zone(config, tmp_path, filed, stale):
    # 02:00Z on 2 Nov is still 1 Nov in New York, where EDGAR's filing date is decided: a
    # filing dated 1 Nov can have been accepted after this fetch.
    _issuer_with_new_filing(config, filed=filed)
    write_archive(config, A, submissions_main(
        cik=A, filings=[(K, "2024-07-30T20:06:22.000Z", "10-K")]),
        now=datetime(2026, 11, 2, 2, tzinfo=timezone.utc))
    fetcher = FakeFetcher({MAIN: _main_with_new_filing()}, config=config)
    _run_a(config, tmp_path / "l", fetcher)
    assert bool(fetcher.calls) is stale


@pytest.mark.parametrize("kw", [{"offline": True}, {"write": False}], ids=["offline", "dry-run"])
def test_offline_and_dry_run_count_a_stale_archive_and_never_fetch(config, tmp_path, kw):
    _issuer_with_new_filing(config)
    fetcher = FakeFetcher({MAIN: _main_with_new_filing()}, config=config)
    out = _run_a(config, tmp_path / "l", fetcher, **kw)
    assert out.status == "ok" and fetcher.calls == []
    assert out.counters["submissions_stale"] == 1
    assert out.counters["known_at_fallback"] == 1
    assert not out.counters["submissions_refreshed:stale"]


def test_a_failed_refresh_fails_that_issuer_and_keeps_its_archive(config, tmp_path):
    _issuer_with_new_filing(config)
    _issuer(config, B)
    before = load_archive(config, A).fetched_at
    fetcher = FakeFetcher({}, config=config)
    rep = store_batches([A, B], config=config, landing=tmp_path / "l", write=True, run_id="r",
                        now=AFTER_FETCH, fetcher=fetcher)
    assert [o.status for o in rep.issuers] == ["failed", "ok"]
    assert rep.issuers[0].error.startswith("submissions_fetch_failed")
    assert not rep.issuers[0].batches and rep.issuers[1].batches
    assert load_archive(config, A).fetched_at == before


def test_a_refresh_that_does_not_list_the_filing_yet_keeps_the_fallback(config, tmp_path):
    _issuer_with_new_filing(config)
    fetcher = FakeFetcher({MAIN: json.dumps(submissions_main(
        cik=A, filings=[(K, "2024-07-30T20:06:22.000Z", "10-K")]))}, config=config)
    out = _run_a(config, tmp_path / "l", fetcher)
    assert out.status == "ok" and len(fetcher.calls) == 1
    assert out.counters["submissions_refreshed:stale"] == 1
    assert out.counters["known_at_fallback"] == 1


def test_a_listed_filing_with_a_malformed_acceptance_time_is_not_stale(config, tmp_path):
    _issuer_with_new_filing(config)
    write_archive(config, A, submissions_main(cik=A, filings=[
        (K, "2024-07-30T20:06:22.000Z", "10-K"), (NEW, "not a time", "10-Q")]))
    fetcher = FakeFetcher({}, config=config)
    out = _run_a(config, tmp_path / "l", fetcher)
    assert fetcher.calls == [] and out.counters["known_at_fallback"] == 1


def test_a_filing_that_gives_no_row_never_refreshes(config, tmp_path):
    _issuer_with_new_filing(config, tag="SomeTagOutsideTheCuratedConcepts")
    fetcher = FakeFetcher({}, config=config)
    out = _run_a(config, tmp_path / "l", fetcher)
    assert out.status == "ok" and fetcher.calls == []
    assert not out.counters["submissions_stale"]


def test_an_explicit_refresh_is_not_counted_as_stale(config, tmp_path):
    _issuer_with_new_filing(config)
    fetcher = FakeFetcher({MAIN: _main_with_new_filing()}, config=config)
    rep = store_batches([A], config=config, landing=tmp_path / "l", write=True, refresh=True,
                        run_id="r", now=AFTER_FETCH, fetcher=fetcher)
    assert len(fetcher.calls) == 1
    assert not rep.issuers[0].counters["submissions_refreshed:stale"]


def test_a_corrupt_archive_error_says_how_to_repair_it(config, tmp_path):
    _issuer(config, A)
    (archive_dir(config, A) / f"CIK{A}.json").write_text("{}", encoding="utf-8")
    out = _run_a(config, tmp_path / "l", FakeFetcher({}, config=config), now=NOW)
    assert out.status == "failed" and out.error.startswith("submissions_archive_corrupt")
    assert "--refresh-submissions --write" in out.error


def test_the_fallback_zone_knob_does_not_move_the_stale_rule(config, tmp_path):
    # The fetch is 1 Nov in New York, 2 Nov in Tokyo: only store_edgar_day_tz decides.
    config.store_known_at_fallback_tz = "Asia/Tokyo"
    _issuer_with_new_filing(config, filed="2026-11-01")
    write_archive(config, A, submissions_main(
        cik=A, filings=[(K, "2024-07-30T20:06:22.000Z", "10-K")]),
        now=datetime(2026, 11, 2, 2, tzinfo=timezone.utc))
    fetcher = FakeFetcher({MAIN: _main_with_new_filing()}, config=config)
    _run_a(config, tmp_path / "l", fetcher)
    assert len(fetcher.calls) == 1


def test_a_filing_date_far_ahead_of_the_run_day_never_refreshes(config, tmp_path):
    _issuer_with_new_filing(config, filed="2026-11-20")     # run day 3 Nov, limit 7 days
    fetcher = FakeFetcher({MAIN: _main_with_new_filing()}, config=config)
    out = _run_a(config, tmp_path / "l", fetcher)
    assert fetcher.calls == [] and out.status == "ok"
    assert not out.counters["submissions_stale"]


def test_a_filing_date_within_the_allowed_days_ahead_still_refreshes(config, tmp_path):
    _issuer_with_new_filing(config, filed="2026-11-10")     # exactly run day + 7
    fetcher = FakeFetcher({MAIN: _main_with_new_filing()}, config=config)
    _run_a(config, tmp_path / "l", fetcher)
    assert len(fetcher.calls) == 1


def test_an_old_archive_with_a_filing_from_yesterday_still_refreshes(config, tmp_path):
    _issuer_with_new_filing(config, filed="2026-11-02")
    write_archive(config, A, submissions_main(
        cik=A, filings=[(K, "2024-07-30T20:06:22.000Z", "10-K")]),
        now=datetime(2026, 8, 5, 9, tzinfo=timezone.utc))   # 90 days before the run
    fetcher = FakeFetcher({MAIN: _main_with_new_filing()}, config=config)
    out = _run_a(config, tmp_path / "l", fetcher)
    assert len(fetcher.calls) == 1 and out.counters["submissions_refreshed:stale"] == 1
