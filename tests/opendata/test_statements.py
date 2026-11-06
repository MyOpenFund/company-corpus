import io
import json
import os
import zipfile
from datetime import date, datetime, timezone

import pytest
import requests

from company_corpus.config import Config
from company_corpus.opendata.statements import (
    archive_root,
    ended_quarters,
    fetch_statements,
    load_statements,
    refresh_statements,
    statement_rows,
    statements_path,
)

NOW = datetime(2009, 8, 1, 12, tzinfo=timezone.utc)    # 2009q1 expected; 2009q2 within grace
A, ACCN = "0000000042", "0000000042-09-000001"
SUB = "adsh\tcik\tname\tform\tperiod\tfiled\taccepted\n" \
      f"{ACCN}\t42\tACME\t10-K\t20081231\t20090301\t2009-03-01 17:00:00.0\n" \
      "0000000099-09-000001\t99\tOTHER\t10-K\t20081231\t20090301\t2009-03-01 17:00:00.0\n"
PRE = "adsh\treport\tline\tstmt\tinpth\trfile\ttag\tversion\tplabel\tnegating\n" \
      f"{ACCN}\t2\t1\tBS\t0\tH\tAssets\tus-gaap/2008\tTotal assets\t0\n" \
      f"{ACCN}\t9\t1\tBS\t0\tH\tAssets\tus-gaap/2008\tAssets, schedule\t0\n" \
      f"{ACCN}\t3\t1\tBS\t1\tH\tGoodwill\tus-gaap/2008\tparenthetical\t0\n" \
      f"{ACCN}\t4\t1\tEQ\t0\tH\tStockholdersEquity\tus-gaap/2008\tequity\t0\n" \
      f"{ACCN}\t5\t1\tIS\t0\tH\tRevenues\tus-gaap/2008\tRevenue\t0\n" \
      "0000000099-09-000001\t2\t1\tBS\t0\tH\tAssets\tus-gaap/2008\tTotal\t0\n"


def _zip(sub=SUB, pre=PRE) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("sub.txt", sub)
        zf.writestr("pre.txt", pre)
    return out.getvalue()


class _Resp:
    def __init__(self, body):
        self.body = body

    def iter_content(self, size):
        yield from (self.body[i:i + size] for i in range(0, len(self.body), size))


class _Fetcher:
    """routes: quarter -> bytes; anything else is a 404."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, stream=False, **_):
        assert stream
        self.calls.append(url)
        for quarter, body in self.routes.items():
            if f"/{quarter}.zip" in url:
                return _Resp(body)
        response = requests.Response()
        response.status_code = 404
        raise requests.HTTPError("404", response=response)


def _run(config, routes, **kw):
    return fetch_statements([A], config=config, write=True, fetcher=_Fetcher(routes), now=NOW, **kw)


def test_quarters_split_into_expected_and_within_grace():
    assert ended_quarters(date(2009, 8, 1), 45) == (["2009q1"], ["2009q2"])
    assert ended_quarters(date(2009, 3, 31), 45) == ([], [])


def test_a_write_archives_with_a_manifest_and_extracts_one_issuer(config):
    rep = _run(config, {"2009q1": _zip()})
    assert rep.errors == [] and rep.fetched == 1 and rep.not_published_yet == ["2009q2"]
    assert (archive_root(config) / "2009q1.zip").is_file()
    manifest = json.loads((archive_root(config) / "manifest.json").read_text())
    assert set(manifest) == {"2009q1"} and len(manifest["2009q1"]["sha256"]) == 64
    rows = load_statements(config, A)
    assert [(r["statement"], r["tag"], r["plabel"]) for r in rows] == [
        ("BS", "Assets", "Total assets"), ("IS", "Revenues", "Revenue")]
    assert rows[0]["accn"] == ACCN and rows[0]["form"] == "10-K" and rows[0]["period"] == "20081231"


def test_the_extraction_is_deterministic_and_replay_fetches_nothing(config):
    _run(config, {"2009q1": _zip()})
    first = statements_path(config, A).read_bytes()
    fetcher = _Fetcher({})
    rep = fetch_statements([A], config=config, write=True, fetcher=fetcher, now=NOW)
    assert statements_path(config, A).read_bytes() == first
    assert rep.present == 1 and rep.fetched == 0
    assert [c for c in fetcher.calls if "2009q1" in c] == []


def test_a_missing_expected_quarter_fails_and_extracts_nothing(config):
    rep = _run(config, {})
    assert rep.errors == ["2009q1: expected and missing (404)"]
    assert load_statements(config, A) is None


@pytest.mark.parametrize("body", [
    b"not a zip",
    _zip()[:40],
])
def test_an_unreadable_zip_fails_and_leaves_nothing_behind(config, body):
    rep = _run(config, {"2009q1": body})
    assert rep.errors and "2009q1" in rep.errors[0]
    assert list(archive_root(config).glob("*.zip*")) == []


def test_a_zip_without_a_member_fails(config):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("sub.txt", SUB)
    rep = _run(config, {"2009q1": out.getvalue()})
    assert "pre.txt" in rep.errors[0]


def test_a_missing_column_fails_and_writes_no_issuer_file(config):
    rep = _run(config, {"2009q1": _zip(pre="adsh\treport\n1\t2\n")})
    assert "columns" in rep.errors[0] and load_statements(config, A) is None


def test_dry_run_lists_what_is_missing_and_writes_nothing(config):
    rep = fetch_statements([A], config=config, write=False, now=NOW)
    assert rep.to_fetch == ["2009q1", "2009q2"] and rep.issuers == 0
    assert not archive_root(config).exists()


def test_archives_can_be_dropped_after_extraction(tmp_path):
    cfg = Config(data_dir=tmp_path, statements_keep_archives=False)
    _run(cfg, {"2009q1": _zip()})
    assert load_statements(cfg, A) and list(archive_root(cfg).glob("*.zip")) == []


def test_refresh_waits_for_the_first_deliberate_download(config):
    fetcher = _Fetcher({"2009q1": _zip()})
    rep = refresh_statements([A], config=config, fetcher=fetcher, now=NOW)
    assert rep.errors == [] and fetcher.calls == [] and load_statements(config, A) is None


def test_refresh_fetches_a_newly_published_quarter_and_rebuilds_the_issuer(config):
    _run(config, {"2009q1": _zip()})
    pre = PRE + f"{ACCN}\t6\t1\tCF\t0\tH\tNetCashProvidedByUsedInOperatingActivities\tv\tCFO\t0\n"
    os.utime(statements_path(config, A), (1, 1))
    fetcher = _Fetcher({"2009q2": _zip(pre=pre)})
    rep = refresh_statements([A], config=config, fetcher=fetcher, now=NOW)
    assert rep.errors == [] and rep.fetched == 1
    assert "NetCashProvidedByUsedInOperatingActivities" in {r["tag"] for r in load_statements(config, A)}


def test_refresh_leaves_a_fresh_issuer_file_alone(config):
    _run(config, {"2009q1": _zip()})
    rep = refresh_statements([A], config=config, fetcher=_Fetcher({}), now=NOW)
    assert rep.errors == [] and rep.issuers == 0 and rep.not_published_yet == ["2009q2"]


def test_refresh_reports_a_missing_expected_quarter(config):
    _run(config, {"2009q1": _zip()})
    later = datetime(2009, 9, 1, tzinfo=timezone.utc)
    rep = refresh_statements([A], config=config, fetcher=_Fetcher({}), now=later)
    assert rep.errors == ["2009q2: expected and missing (404)"]


def test_rows_take_the_acceptance_time_else_the_filing_day(config):
    rows = [{"accn": ACCN, "form": "10-K", "period": "20081231", "filed": "20090301",
             "statement": "BS", "tag": "Assets", "version": "us-gaap/2008", "plabel": "Total"}]
    accepted = datetime(2009, 3, 1, 22, 0, tzinfo=timezone.utc)
    res = statement_rows(A, rows, acceptance={ACCN: accepted}, manifest_doc_ids=set(), config=config, now=NOW)
    (r,) = res.rows
    assert r["known_at"] == accepted and r["valid_date"] == date(2008, 12, 31)
    assert r["filing_ref"] == ACCN and r["source_doc_id"] and r["source_code"] == "sec"
    (fallback,) = statement_rows(A, rows, acceptance={}, manifest_doc_ids=set(), config=config, now=NOW).rows
    assert fallback["known_at"].date() in (date(2009, 3, 1), date(2009, 3, 2))


def test_a_bad_period_is_counted_not_fatal(config):
    rows = [{"accn": ACCN, "form": "10-K", "period": "", "filed": "20090301", "statement": "BS",
             "tag": "Assets", "version": "v", "plabel": "p"}]
    res = statement_rows(A, rows, acceptance={}, manifest_doc_ids=set(), config=config, now=NOW)
    assert res.rows == [] and res.counters["statements_dropped:bad_period"] == 1
