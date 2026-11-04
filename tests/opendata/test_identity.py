from datetime import date

from company_corpus.config import DEFAULT_EDGAR_COUNTRY_CODES
from company_corpus.opendata.identity import identity_rows
from tests.opendata.factories import FETCHED, submissions_main, write_archive

CIK = "0000000042"


def _rows(config, **kw):
    a = write_archive(config, CIK, submissions_main(**kw))
    return identity_rows(a, country_codes=DEFAULT_EDGAR_COUNTRY_CODES)


def _by(rows, scheme):
    return sorted((r for r in rows if r["id_scheme"] == scheme), key=lambda r: r["valid_date"])


def test_current_name_country_and_constants(config):
    res = _rows(config, name="ACME CORP")
    (cik,) = _by(res.rows, "cik")
    assert cik == {
        "subject_scheme": "cik", "subject_value": CIK, "id_scheme": "cik", "id_value": CIK,
        "name": "ACME CORP", "country": "US", "valid_date": FETCHED.date(),
        "known_at": FETCHED, "known_at_kind": "collected", "source_code": "sec",
        "source_doc_id": None,
    }


def test_multi_class_issuer_gets_one_row_per_ticker(config):
    res = _rows(config, tickers=("GOOGL", "GOOG"))
    assert [(r["id_value"], r["name"], r["valid_date"]) for r in _by(res.rows, "ticker")] in (
        [("GOOG", None, FETCHED.date()), ("GOOGL", None, FETCHED.date())],
        [("GOOGL", None, FETCHED.date()), ("GOOG", None, FETCHED.date())],
    )
    assert {r["subject_value"] for r in res.rows} == {CIK}


def test_dated_former_names(config):
    res = _rows(config, name="META PLATFORMS, INC.",
                former=[("FACEBOOK INC", "2004-07-29T00:00:00.000Z", "2021-10-28T00:00:00.000Z")])
    assert [(r["name"], r["valid_date"]) for r in _by(res.rows, "cik")] == [
        ("FACEBOOK INC", date(2004, 7, 29)),
        ("META PLATFORMS, INC.", date(2021, 10, 28)),
    ]


def test_a_former_name_on_the_current_name_date_is_shadowed(config):
    # EDGAR data seen at Medtronic: a zero-length former name ending when the current one starts.
    res = _rows(config, name="MEDTRONIC PLC",
                former=[("MEDTRONIC LTD", "2015-01-15T00:00:00.000Z", "2015-01-15T00:00:00.000Z"),
                        ("MEDTRONIC HOLDINGS", "2014-07-14T00:00:00.000Z", "2014-11-21T00:00:00.000Z")])
    assert [(r["name"], r["valid_date"]) for r in _by(res.rows, "cik")] == [
        ("MEDTRONIC HOLDINGS", date(2014, 7, 14)),
        ("MEDTRONIC PLC", date(2015, 1, 15)),
    ]
    assert res.counters["former_name_shadowed"] == 1


def test_foreign_address_through_the_table_and_unknown_code(config):
    assert _by(_rows(config, foreign_code="L2").rows, "cik")[0]["country"] == "IE"
    res = _rows(config, foreign_code="Q9")
    assert _by(res.rows, "cik")[0]["country"] is None
    assert res.counters["country_unknown"] == 1 and res.counters["country_unknown:Q9"] == 1


def test_undated_former_name_is_counted_not_guessed(config):
    res = _rows(config, former=[("OLD NAME", None, "2020-01-01T00:00:00.000Z")])
    assert res.counters["former_name_undated"] == 1
    assert [r["name"] for r in _by(res.rows, "cik")] == ["ACME CORP"]


def test_rows_are_unique_on_the_natural_key(config):
    res = _rows(config, tickers=("ACME", "ACME", " "),
                former=[("A", "2001-01-01T00:00:00.000Z", "2002-01-01T00:00:00.000Z"),
                        ("B", "2001-01-01T00:00:00.000Z", "2003-01-01T00:00:00.000Z")])
    keys = [(r["id_scheme"], r["id_value"], r["valid_date"]) for r in res.rows]
    assert len(keys) == len(set(keys))
    assert ("B", date(2001, 1, 1)) in [(r["name"], r["valid_date"]) for r in res.rows]
