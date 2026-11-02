import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("myopendata")

from company_corpus.opendata.batches import store_batches  # noqa: E402
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
    assert rep.issuers[0].rows == {"entity_observations_v1": 2, "fundamentals_v2": 2}
    batches = sorted(p.parent.parent.name for p in (tmp_path / "landing").glob("*/*/batch.json"))
    assert batches == ["entity_observations_v1"] * 2 + ["fundamentals_v2"] * 2
    m = json.loads(next((tmp_path / "landing" / "fundamentals_v2").glob("*/batch.json"))
                   .read_text())
    assert m["producer"] == "company-corpus"
    assert any(s.startswith("raw/0000000042/F1/companyfacts.json@sha256:")
               or s.startswith("raw/0000000043/F1/companyfacts.json@sha256:")
               for s in m["source_inputs"])


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
    assert b1["fundamentals_v2"] == b2["fundamentals_v2"]
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
    assert t["rows"] == {"entity_observations_v1": 2, "fundamentals_v2": 2}
    d = rep.to_details()["store_batches"]
    assert d["write"] is True and d["issuers"][B]["error"] == "companyfacts_missing"
