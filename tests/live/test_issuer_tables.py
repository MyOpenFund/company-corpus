"""Live issuer tables equal the ones ``store-batches`` builds from the same SEC bytes."""
import json
from datetime import date, datetime, timezone

import pytest

pytest.importorskip("open_data_store")

from company_corpus.concepts.selection import run_selection  # noqa: E402
from company_corpus.live import issuer as live_issuer  # noqa: E402
from company_corpus.live.issuer import IssuerTables, issuer_tables  # noqa: E402
from company_corpus.live.sec import SecHttpError, SecNotFound, SecRefused, SecUnavailable  # noqa: E402
from company_corpus.opendata import batches  # noqa: E402
from company_corpus.opendata.batches import store_batches  # noqa: E402
from company_corpus.opendata.statements import statements_path  # noqa: E402
from company_corpus.sources.edgar_submissions import SUBMISSIONS_URL  # noqa: E402
from company_corpus.sources.edgar_xbrl import COMPANYFACTS_URL  # noqa: E402
from tests.opendata.factories import (  # noqa: E402
    companyfacts, point, submissions_main, write_archive, write_companyfacts)

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
CIK = "0000000042"
REVENUE = 391035000000
TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"
K24, Q24, K23, E24 = ("0000000042-24-000010", "0000000042-24-000005",
                      "0000000042-23-000010", "0000000042-24-000011")
FILINGS = [  # (accession, acceptance, form, filingDate, reportDate)
    (E24, "2024-11-05T21:00:00.000Z", "8-K", "2024-11-05", "2024-10-31"),
    (K24, "2024-11-01T20:06:22.000Z", "10-K", "2024-11-01", "2024-09-28"),
    (Q24, "2024-08-02T20:00:00.000Z", "10-Q", "2024-08-02", "2024-06-29"),
    (K23, "2023-11-03T20:00:00.000Z", "10-K", "2023-11-03", "2023-09-30"),
]


def _facts():
    return companyfacts({
        TAG: {"USD": [
            point(REVENUE, "2024-09-28", start="2023-10-01", accn=K24, filed="2024-11-01"),
            point(383285000000, "2023-09-30", start="2022-10-02", accn=K23, filed="2023-11-03",
                  fy=2023),
            point(85777000000, "2024-06-29", start="2024-03-31", accn=Q24, form="10-Q",
                  filed="2024-08-02", fp="Q3")]},
        "Assets": {"USD": [point(364980000000, "2024-09-28", accn=K24, filed="2024-11-01")]}})


def _position(accn, form, period, filed, accepted, statement, tag, report, line):
    return {"accn": accn, "form": form, "period": period, "filed": filed, "accepted": accepted,
            "statement": statement, "tag": tag, "version": "us-gaap/2024", "plabel": "",
            "report": report, "line": line}


POSITIONS = {  # per accession, as filing_positions returns them (sorted by (accn, stmt, tag))
    K24: [_position(K24, "10-K", "20240930", "20241101", "2024-11-01 20:06:00.0", "BS",
                    "Assets", 2, 12),
          _position(K24, "10-K", "20240930", "20241101", "2024-11-01 20:06:00.0", "IS",
                    TAG, 4, 3)],
    Q24: [_position(Q24, "10-Q", "20240630", "20240802", "2024-08-02 20:00:00.0", "IS",
                    TAG, 4, 3)],
    K23: [_position(K23, "10-K", "20230930", "20231103", "2023-11-03 20:00:00.0", "IS",
                    TAG, 4, 3)],
}


class FakeSec:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_bytes(self, url, *, max_age_s=None):
        self.calls.append((url, max_age_s))
        if url not in self.routes:
            raise SecNotFound(url)
        return self.routes[url]


def _corpus(config):
    """The issuer laid out as store-batches reads it; returns the bytes the SEC would serve."""
    facts_path = write_companyfacts(config, CIK, _facts())
    write_archive(config, CIK, submissions_main(cik=CIK, filings=FILINGS), now=NOW)
    rows = [r for accn in POSITIONS for r in POSITIONS[accn]]
    rows.sort(key=lambda r: (r["accn"], r["statement"], r["tag"]))  # as _extract writes them
    path = statements_path(config, CIK)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")
    main = config.raw_dir / CIK / "submissions" / f"CIK{CIK}.json"
    return {SUBMISSIONS_URL.format(cik=CIK): main.read_bytes(),
            COMPANYFACTS_URL.format(cik=CIK): facts_path.read_bytes()}


@pytest.fixture
def fake_positions(monkeypatch):
    calls = []

    def positions(ref, sec, *, counters=None):
        calls.append(ref)
        return [dict(r) for r in POSITIONS.get(ref.accession, [])]

    monkeypatch.setattr(live_issuer, "filing_positions", positions)
    return calls


def _live(config, sec, **kw) -> IssuerTables:
    kw = {"positions_from": None, "positions_to": None, "facts_max_age_s": None,
          "submissions_max_age_s": None, **kw}
    return issuer_tables(CIK, sec, config=config, now=NOW, **kw)


def _store_tables(config, tmp_path, monkeypatch) -> dict:
    """The tables store-batches builds (dry-run, offline), captured at its contract check."""
    captured, real = {}, batches._store_api

    def spy():
        mod = real()
        conform = mod.conform

        def capture(contract, table, *, now):
            captured[contract.table_name] = table
            return conform(contract, table, now=now)

        mod.conform = capture
        return mod

    monkeypatch.setattr(batches, "_store_api", spy)
    rep = store_batches([CIK], config=config, landing=tmp_path / "landing", write=False,
                        offline=True, run_id="r", now=NOW)
    assert rep.issuers[0].status == "ok", rep.issuers[0].error
    return captured


def test_rows_equal_store_batches_offline(config, tmp_path, monkeypatch, fake_positions):
    sec = FakeSec(_corpus(config))
    store = _store_tables(config, tmp_path, monkeypatch)
    t = _live(config, sec)
    live = {"entity_observations_v1": t.entity_observations, "fundamentals_v3": t.fundamentals,
            "filings_v1": t.filings, "filing_statements_v1": t.filing_statements}
    assert set(store) == set(live)
    for name, table in live.items():
        assert table.num_rows > 0, name
        assert table.equals(store[name]), name


def test_selection_runs_on_live_tables(config, fake_positions):
    t = _live(config, FakeSec(_corpus(config)))
    rows = run_selection(t.fundamentals, t.filing_statements)
    fy2024 = [r for r in rows if r["concept"] == "revenue" and r["frequency"] == "annual"
              and r["valid_date"] == date(2024, 9, 28)]
    assert len(fy2024) == 1
    assert fy2024[0]["value"] == REVENUE and fy2024[0]["filing_ref"] == K24
    assert fy2024[0]["location"] == "statement" and fy2024[0]["on_statement"]


def test_positions_only_in_window(config, fake_positions):
    _live(config, FakeSec(_corpus(config)),
          positions_from=date(2024, 1, 1), positions_to=date(2024, 12, 31))
    assert sorted(r.accession for r in fake_positions) == sorted([K24, Q24])  # not 8-K, not 2023


def test_latest_filing_is_the_newest_of_any_form(config, fake_positions):
    t = _live(config, FakeSec(_corpus(config)))
    assert t.latest_filing is not None and t.latest_filing.accession == E24
    assert t.cik == CIK


def test_max_ages_reach_the_sec(config, fake_positions):
    sec = FakeSec(_corpus(config))
    _live(config, sec, facts_max_age_s=60.0, submissions_max_age_s=5.0)
    assert dict(sec.calls) == {SUBMISSIONS_URL.format(cik=CIK): 5.0,
                               COMPANYFACTS_URL.format(cik=CIK): 60.0}


def test_companyfacts_404_raises_not_found(config, fake_positions):
    routes = _corpus(config)
    del routes[COMPANYFACTS_URL.format(cik=CIK)]
    with pytest.raises(SecNotFound):
        _live(config, FakeSec(routes))


def test_a_filing_whose_positions_fail_is_counted_and_skipped(config, monkeypatch):
    def positions(ref, sec, *, counters=None):
        if ref.accession == Q24:
            raise SecUnavailable("throttled")
        if ref.accession == K23:
            raise SecNotFound("gone")
        counters["positions:seen"] += 1
        return [dict(r) for r in POSITIONS.get(ref.accession, [])]

    monkeypatch.setattr(live_issuer, "filing_positions", positions)
    t = _live(config, FakeSec(_corpus(config)))
    assert t.counters["positions:fetch_failed"] == 2
    assert t.counters["positions:seen"] == 1
    assert set(t.filing_statements.column("filing_ref").to_pylist()) == {K24}


def test_a_filing_answered_with_another_http_error_is_counted_and_skipped(config, monkeypatch):
    def positions(ref, sec, *, counters=None):
        if ref.accession == Q24:
            raise SecHttpError(410, "gone for good")
        counters["positions:seen"] += 1
        return [dict(r) for r in POSITIONS.get(ref.accession, [])]

    monkeypatch.setattr(live_issuer, "filing_positions", positions)
    t = _live(config, FakeSec(_corpus(config)))
    assert t.counters["positions:fetch_failed"] == 1
    assert t.counters["positions:seen"] == 2
    assert set(t.filing_statements.column("filing_ref").to_pylist()) == {K24, K23}


def test_refused_propagates(config, monkeypatch):
    def positions(ref, sec, *, counters=None):
        raise SecRefused("undeclared")

    monkeypatch.setattr(live_issuer, "filing_positions", positions)
    with pytest.raises(SecRefused):
        _live(config, FakeSec(_corpus(config)))


def test_an_issuer_without_filings_gives_empty_typed_tables(config, fake_positions):
    write_archive(config, CIK, submissions_main(cik=CIK, filings=[]), now=NOW)
    main = (config.raw_dir / CIK / "submissions" / f"CIK{CIK}.json").read_bytes()
    sec = FakeSec({SUBMISSIONS_URL.format(cik=CIK): main,
                   COMPANYFACTS_URL.format(cik=CIK): json.dumps(companyfacts({})).encode()})
    t = _live(config, sec)
    assert t.latest_filing is None and t.fundamentals.num_rows == 0
    assert t.filing_statements.num_rows == 0 and "known_at" in t.fundamentals.schema.names
    assert not fake_positions
