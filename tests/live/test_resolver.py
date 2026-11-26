import json
from datetime import date
from pathlib import Path

from company_corpus.live.resolver import TICKERS_URL, former_name_rows, resolver_rows
from company_corpus.opendata.submissions import archive_from_bytes
from tests.opendata.factories import FETCHED, submissions_main

TICKERS = Path(__file__).resolve().parent.parent / "fixtures" / "live" / "documents" / "company_tickers.json"
KEYS = {"subject_value", "name", "id_scheme", "id_value", "valid_date", "entity_id", "country"}


class FakeSec:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_bytes(self, url, *, max_age_s=None):
        self.calls.append((url, max_age_s))
        return self.routes[url]


def test_resolver_rows_shape():
    sec = FakeSec({TICKERS_URL: TICKERS.read_bytes()})
    rows = resolver_rows(sec, max_age_s=3600.0)
    assert TICKERS_URL == "https://www.sec.gov/files/company_tickers.json"
    assert sec.calls == [(TICKERS_URL, 3600.0)]  # the caller's TTL goes through
    msft = [r for r in rows if r["id_value"] == "MSFT"]
    assert msft == [{"subject_value": "0000789019", "name": "MICROSOFT CORP", "id_scheme": "ticker",
                     "id_value": "MSFT", "valid_date": None, "entity_id": None, "country": None}]
    assert all(set(r) == KEYS for r in rows)
    assert [r["id_value"] for r in rows if r["subject_value"] == "0001652044"] == ["GOOGL", "GOOG"]


def test_resolver_rows_skip_malformed_entries():
    body = {"0": {"cik_str": 1, "ticker": "A", "title": "A CO"}, "1": {"ticker": "NOCIK"},
            "2": "junk", "3": {"cik_str": "x", "ticker": "B", "title": "B"},
            "4": {"cik_str": "0000000002", "ticker": " ", "title": "C"}}
    sec = FakeSec({TICKERS_URL: json.dumps(body).encode()})
    assert [r["id_value"] for r in resolver_rows(sec, max_age_s=None)] == ["A"]


def _archive(main):
    return archive_from_bytes("0000000042", json.dumps(main).encode(), [], fetched_at=FETCHED,
                              url_of=lambda n: n)


def test_former_name_rows():
    main = submissions_main(name="NEW CO", former=[
        ("OLD CO", "2001-02-03T05:00:00.000Z", "2010-05-06T04:00:00.000Z"),
        ("MID CO", "2010-05-06T04:00:00.000Z", "2020-01-02T05:00:00.000Z"),
        ("", "2000-01-01T00:00:00.000Z", None)])
    rows = former_name_rows(_archive(main))
    assert all(set(r) == KEYS and r["id_scheme"] is None and r["id_value"] is None for r in rows)
    assert [(r["name"], r["valid_date"]) for r in rows] == [
        ("OLD CO", date(2001, 2, 3)), ("MID CO", date(2010, 5, 6)), ("NEW CO", date(2020, 1, 2))]
    assert {r["subject_value"] for r in rows} == {"0000000042"}


def test_former_name_rows_without_former_names():
    rows = former_name_rows(_archive(submissions_main(name="ACME CORP")))
    assert [(r["name"], r["valid_date"]) for r in rows] == [("ACME CORP", FETCHED.date())]
