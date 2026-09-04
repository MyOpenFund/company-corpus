"""Tests for the Denmark OAM backend (OamDK / Finanstilsynet OAM Publication API).

All network-free: a stub fetcher routes get_json (/config, /details/) and
post_json (/search) from the captured fixtures in tests/fixtures/eu/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_dk import OamDK, _MAX_PAGES, _doc_type, _normalise

FIX = Path(__file__).parent.parent / "fixtures" / "eu"

NOVO_CVR = "24256790"
NOVO_LEI = "549300DAQ1CVT6CXN342"
NOVO_ENTITY = Entity(lei=NOVO_LEI, name="Novo Nordisk A/S", country="DK")

_BLOB_HOST = "https://saegressprod.blob.core.windows.net"


# ---------------------------------------------------------------------------
# Stub fetcher
# ---------------------------------------------------------------------------

class _StubFetcher:
    """Routes get_json and post_json from fixture files.

    - GET /config  -> dk_config.json
    - GET /details/<any> -> dk_details.json
    - POST /search -> dk_search_novo.json on first call; empty rows + same
      paging (totalPages=1) on subsequent calls so pagination terminates.
    """

    def __init__(self):
        self._config = json.loads((FIX / "dk_config.json").read_text())
        self._search = json.loads((FIX / "dk_search_novo.json").read_text())
        self._details = json.loads((FIX / "dk_details.json").read_text())
        self.get_calls: list[str] = []
        self.post_calls: list[dict] = []

    def get_json(self, url: str, **_):
        self.get_calls.append(url)
        if "/config" in url:
            return self._config
        if "/details/" in url:
            return self._details
        return {}

    def post_json(self, url: str, body: dict, **_):
        self.post_calls.append({"url": url, "body": body})
        if len(self.post_calls) == 1:
            return self._search
        # Subsequent pages: same paging info but no rows → pagination terminates
        return {
            "paging": self._search["paging"],
            "data": {"type": "table", "rows": []},
        }


def _make_stub() -> _StubFetcher:
    return _StubFetcher()


# ---------------------------------------------------------------------------
# test_resolve_by_name_and_discover
# ---------------------------------------------------------------------------

def test_discover_returns_documents_for_novo():
    """discover() resolves Novo Nordisk by name -> CVR and returns Documents."""
    stub = _make_stub()
    src = OamDK(fetcher=stub)

    docs = src.discover(NOVO_ENTITY)

    assert docs, "expected at least one Document from fixture"
    assert not src.errors, f"unexpected errors: {src.errors}"


def test_discover_doc_types_in_DOC_TYPES():
    """Every returned document's doc_type must be a member of DOC_TYPES."""
    stub = _make_stub()
    src = OamDK(fetcher=stub)

    docs = src.discover(NOVO_ENTITY)

    assert all(d.doc_type in DOC_TYPES for d in docs), (
        f"doc_type out of DOC_TYPES: {[d.doc_type for d in docs]}"
    )


def test_discover_file_urls_start_with_blob_host():
    """Every file URL must start with the Azure blob host."""
    stub = _make_stub()
    src = OamDK(fetcher=stub)

    docs = src.discover(NOVO_ENTITY)

    for d in docs:
        for f in d.files:
            assert f["url"].startswith(_BLOB_HOST), (
                f"file URL does not start with blob host: {f['url']}"
            )


def test_discover_source_is_oam_dk():
    """Every Document's source field must be 'oam-dk'."""
    stub = _make_stub()
    src = OamDK(fetcher=stub)

    docs = src.discover(NOVO_ENTITY)

    assert all(d.source == "oam-dk" for d in docs)


def test_discover_lei_propagated():
    """The entity's LEI must be propagated to every Document."""
    stub = _make_stub()
    src = OamDK(fetcher=stub)

    docs = src.discover(NOVO_ENTITY)

    assert all(d.lei == NOVO_LEI for d in docs)


def test_discover_country_is_DK():
    stub = _make_stub()
    src = OamDK(fetcher=stub)
    docs = src.discover(NOVO_ENTITY)
    assert all(d.country == "DK" for d in docs)


# ---------------------------------------------------------------------------
# test_doc_type_mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category,expected", [
    ("YearlyFinancialReport", "annual_report"),
    ("HalfYearlyFinancialReport", "half_year_report"),
    ("QuarterlyFinancialReport", "interim_statement"),
    ("InsideInformation", "inside_information"),
    ("Shareholder", "holding_notification"),
    ("TotalVotingRightsAndShareCapital", "holding_notification"),
    ("Prospectus", "prospectus"),
    ("OwnShares", "other"),
    ("PaymentsToGovernments", "other"),
    ("HomeMemberState", "other"),
    ("ChangeInRightsAttachedToSecurities", "other"),
    ("RelatedPartyTransactions", "other"),
    ("TakeoverBid", "other"),
    ("ShortSelling", "other"),
    ("SomethingUnknown", "other"),
    ("", "other"),
])
def test_doc_type_mapping(category, expected):
    assert _doc_type(category) == expected, (
        f"_doc_type({category!r}) expected {expected!r}, got {_doc_type(category)!r}"
    )


def test_doc_type_case_insensitive():
    assert _doc_type("yearlyfinancialreport") == "annual_report"
    assert _doc_type("YEARLYFINANCIALREPORT") == "annual_report"
    assert _doc_type("yEaRlYfInAnCiAlRePoRt") == "annual_report"


# ---------------------------------------------------------------------------
# test_no_name_match -> [] + error recorded
# ---------------------------------------------------------------------------

def test_no_name_match_returns_empty_and_records_error():
    """An entity whose name does not match any CVR returns [] and records an error."""
    stub = _make_stub()
    src = OamDK(fetcher=stub)

    docs = src.discover(Entity(lei=None, name="NO SUCH COMPANY XYZ 99999", country="DK"))

    assert docs == []
    resolve_errors = [e for e in src.errors if e["context"] == "resolve-name"]
    assert resolve_errors, "expected a resolve-name error for unknown entity"


# ---------------------------------------------------------------------------
# test_search_body_filters_by_cvr
# ---------------------------------------------------------------------------

def test_search_body_contains_correct_cvr():
    """The POST /search body must include the resolved CVR in IssuerFilter.options."""
    stub = _make_stub()
    src = OamDK(fetcher=stub)

    src.discover(NOVO_ENTITY)

    assert stub.post_calls, "expected at least one POST /search"
    first_body = stub.post_calls[0]["body"]
    filters = first_body.get("filters") or []
    issuer_filters = [f for f in filters if f.get("key") == "IssuerFilter"]
    assert issuer_filters, "expected an IssuerFilter in the POST body"
    options = issuer_filters[0].get("options") or []
    assert NOVO_CVR in options, (
        f"expected CVR {NOVO_CVR!r} in IssuerFilter.options; got {options}"
    )


def test_search_body_sorting():
    """The POST body must sort by PublicationDateColumn descending."""
    stub = _make_stub()
    src = OamDK(fetcher=stub)
    src.discover(NOVO_ENTITY)

    body = stub.post_calls[0]["body"]
    sorting = body.get("sorting") or {}
    assert sorting.get("key") == "PublicationDateColumn"
    assert sorting.get("direction") == "descending"


# ---------------------------------------------------------------------------
# test_pagination_terminates
# ---------------------------------------------------------------------------

def test_pagination_terminates_on_last_page():
    """Pagination stops when page >= totalPages."""

    class _MultiPageStub:
        def __init__(self, total_pages: int, rows_per_page: int):
            self._total_pages = total_pages
            self._rows_per_page = rows_per_page
            self._config = json.loads((FIX / "dk_config.json").read_text())
            self._details = json.loads((FIX / "dk_details.json").read_text())
            self.post_calls: list[dict] = []

        def get_json(self, url, **_):
            if "/config" in url:
                return self._config
            return self._details

        def post_json(self, url, body, **_):
            self.post_calls.append(body)
            page = body.get("page", 1)
            rows = [
                {
                    "id": f"id-{page}-{i}",
                    "HeadlineColumn": f"Doc {page}-{i}",
                    "IssuerColumn": "NOVO NORDISK A/S",
                    "CategoryColumn": "YearlyFinancialReport",
                    "PublicationDateColumn": "01-01-2025 00:00:00",
                    "RegistrationDateColumn": "01-01-2025 00:00:00",
                }
                for i in range(self._rows_per_page)
            ] if page <= self._total_pages else []
            return {
                "paging": {
                    "page": page,
                    "pageSize": 100,
                    "totalCount": self._total_pages * self._rows_per_page,
                    "totalPages": self._total_pages,
                },
                "data": {"type": "table", "rows": rows},
            }

    stub = _MultiPageStub(total_pages=3, rows_per_page=2)
    src = OamDK(fetcher=stub)
    docs = src.discover(NOVO_ENTITY)

    # 3 pages × 2 rows × 1 doc per row (details has 3 link files)
    assert len(docs) == 3 * 2, f"expected 6 docs, got {len(docs)}"
    assert len(stub.post_calls) == 3, (
        f"expected 3 POST calls (one per page), got {len(stub.post_calls)}"
    )
    assert not src.errors


def test_pagination_truncation_recorded_when_total_pages_exceeds_max():
    """When totalPages > _MAX_PAGES a 'truncated' error is recorded."""

    class _TruncStub:
        def __init__(self):
            self._config = json.loads((FIX / "dk_config.json").read_text())
            self._details = json.loads((FIX / "dk_details.json").read_text())

        def get_json(self, url, **_):
            if "/config" in url:
                return self._config
            return self._details

        def post_json(self, url, body, **_):
            page = body.get("page", 1)
            # Report many more pages than _MAX_PAGES
            total_pages = _MAX_PAGES + 10
            rows = [{
                "id": f"id-{page}",
                "HeadlineColumn": "Doc",
                "IssuerColumn": "NOVO NORDISK A/S",
                "CategoryColumn": "OwnShares",
                "PublicationDateColumn": "01-01-2025 00:00:00",
                "RegistrationDateColumn": "01-01-2025 00:00:00",
            }] if page == 1 else []
            return {
                "paging": {
                    "page": page,
                    "pageSize": 100,
                    "totalCount": total_pages * 100,
                    "totalPages": total_pages,
                },
                "data": {"type": "table", "rows": rows},
            }

    src = OamDK(fetcher=_TruncStub())
    src.discover(NOVO_ENTITY)

    truncation_errors = [e for e in src.errors if e["context"] == "truncated"]
    assert truncation_errors, "expected a 'truncated' error when totalPages > _MAX_PAGES"


# ---------------------------------------------------------------------------
# test_list_issuers
# ---------------------------------------------------------------------------

def test_list_issuers_returns_empty():
    stub = _make_stub()
    src = OamDK(fetcher=stub)
    assert src.list_issuers() == []


# ---------------------------------------------------------------------------
# test_name + country
# ---------------------------------------------------------------------------

def test_backend_name_and_country():
    src = OamDK(fetcher=_make_stub())
    assert src.name == "oam-dk"
    assert src.country == "DK"


# ---------------------------------------------------------------------------
# test_normalise (CVR resolution)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Novo Nordisk A/S", "novo nordisk"),
    ("NOVO NORDISK A/S", "novo nordisk"),
    ("novo nordisk a/s", "novo nordisk"),
    ("A.P. MØLLER - MÆRSK A/S", "a.p. møller - mærsk"),
    ("  some  company  ", "some company"),
])
def test_normalise(raw, expected):
    assert _normalise(raw) == expected, (
        f"_normalise({raw!r}) expected {expected!r}, got {_normalise(raw)!r}"
    )


# ---------------------------------------------------------------------------
# test_search_error_recorded
# ---------------------------------------------------------------------------

def test_search_error_is_recorded():
    """A POST /search failure records a 'search' error and returns empty list."""

    class _FailSearchStub:
        def get_json(self, url, **_):
            if "/config" in url:
                return json.loads((FIX / "dk_config.json").read_text())
            return {}

        def post_json(self, url, body, **_):
            raise ConnectionError("network down")

    src = OamDK(fetcher=_FailSearchStub())
    docs = src.discover(NOVO_ENTITY)

    assert docs == []
    assert any(e["context"] == "search" for e in src.errors)


# ---------------------------------------------------------------------------
# test_details_error_recorded_but_continues
# ---------------------------------------------------------------------------

def test_details_error_recorded_but_other_rows_continue():
    """A /details fetch failure records an error but does not abort the other rows."""

    class _PartialDetailsStub:
        def __init__(self):
            self._config = json.loads((FIX / "dk_config.json").read_text())
            self._details = json.loads((FIX / "dk_details.json").read_text())
            self._detail_calls = 0

        def get_json(self, url, **_):
            if "/config" in url:
                return self._config
            # First details call fails, subsequent succeed
            self._detail_calls += 1
            if self._detail_calls == 1:
                raise ConnectionError("blob unreachable")
            return self._details

        def post_json(self, url, body, **_):
            return {
                "paging": {"page": 1, "pageSize": 100, "totalCount": 3, "totalPages": 1},
                "data": {"type": "table", "rows": [
                    {
                        "id": f"row-{i}",
                        "HeadlineColumn": f"Doc {i}",
                        "IssuerColumn": "NOVO NORDISK A/S",
                        "CategoryColumn": "YearlyFinancialReport",
                        "PublicationDateColumn": "01-01-2025 00:00:00",
                        "RegistrationDateColumn": "01-01-2025 00:00:00",
                    }
                    for i in range(3)
                ]},
            }

    src = OamDK(fetcher=_PartialDetailsStub())
    docs = src.discover(NOVO_ENTITY)

    # First row fails, rows 1+2 succeed
    assert len(docs) == 2, f"expected 2 docs (one failed), got {len(docs)}"
    assert any(e["context"] == "details" for e in src.errors)


def test_pagination_without_total_pages():
    """Stub returns 2 full pages then empty with NO 'totalPages' field.
    Old code defaulted absent totalPages to 1 → stopped after page 1.
    Fix: absent totalPages drives pagination by empty rows instead."""

    class _NoTotalPageStub:
        def __init__(self):
            self._config = json.loads((FIX / "dk_config.json").read_text())
            self._details = json.loads((FIX / "dk_details.json").read_text())
            self.post_calls: list[dict] = []

        def get_json(self, url, **_):
            if "/config" in url:
                return self._config
            return self._details

        def post_json(self, url, body, **_):
            self.post_calls.append(body)
            page = body.get("page", 1)
            if page <= 2:
                rows = [
                    {
                        "id": f"row-{page}-{i}",
                        "HeadlineColumn": f"Doc {page}-{i}",
                        "IssuerColumn": "NOVO NORDISK A/S",
                        "CategoryColumn": "YearlyFinancialReport",
                        "PublicationDateColumn": "01-01-2025 00:00:00",
                        "RegistrationDateColumn": "01-01-2025 00:00:00",
                    }
                    for i in range(3)
                ]
            else:
                rows = []  # empty page → terminates
            return {
                "paging": {
                    "page": page,
                    "pageSize": 100,
                    "totalCount": 6,
                    # NO 'totalPages' key
                },
                "data": {"type": "table", "rows": rows},
            }

    stub = _NoTotalPageStub()
    src = OamDK(fetcher=stub)
    docs = src.discover(NOVO_ENTITY)

    # 2 pages × 3 rows = 6 docs expected
    assert len(docs) == 6, (
        f"expected 6 docs from 2 pages without totalPages, got {len(docs)}"
    )
    assert len(stub.post_calls) == 3, (
        f"expected 3 POST calls (page1, page2, empty page3), got {len(stub.post_calls)}"
    )
    assert not src.errors


def test_doc_type_maps_english_detail_labels():
    """Live path: the search row's CategoryColumn is 'Udsteder'; the real category is
    the English label exposed in /details. Both forms must map."""
    from company_corpus.eu.sources.oam_dk import _doc_type, _category_from_detail
    assert _doc_type("Annual financial report") == "annual_report"
    assert _doc_type("Half-yearly financial report") == "half_year_report"
    assert _doc_type("Inside information") == "inside_information"
    assert _doc_type("Udsteder") == "other"          # the useless row value
    assert _doc_type("YearlyFinancialReport") == "annual_report"  # old API key still works

    detail = {"sections": [{"heading": "Notification", "elements": [
        {"type": "keyvalue", "key": {"name": "Type"}, "value": {"type": "text", "text": "Issuer"}},
        {"type": "keyvalue", "key": {"name": ""}, "value": {"type": "text", "text": "Inside information"}},
    ]}]}
    assert _category_from_detail(detail) == "Inside information"
    assert _doc_type(_category_from_detail(detail)) == "inside_information"
