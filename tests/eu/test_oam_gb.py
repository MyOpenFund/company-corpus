"""Tests for the UK OAM backend (NsmGB / FCA National Storage Mechanism).

All network-free: a stub fetcher routes post_json from the captured fixture
``gb_nsm_tesco.json`` and synthetic data.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_gb import NsmGB, _MAX_RESULTS, _PAGE, _doc_type

FIX = Path(__file__).parent.parent / "fixtures" / "eu"

TESCO_LEI = "2138002P5RNKC5W2JZ46"
TESCO_ENTITY = Entity(lei=TESCO_LEI, name="Tesco PLC", country="GB")


# ---------------------------------------------------------------------------
# Stub fetcher
# ---------------------------------------------------------------------------

class _StubFetcher:
    """Returns the fixture on the first post_json call; empty hits page thereafter."""

    def __init__(self, fixture_text: str):
        self._fixture = json.loads(fixture_text)
        self.calls: list[dict] = []  # record every (url, body) pair

    def post_json(self, url: str, body: dict, **_):
        self.calls.append({"url": url, "body": body})
        if len(self.calls) == 1:
            return self._fixture
        # Subsequent pages: same total, no hits → pagination terminates
        total_val = self._fixture["hits"]["total"]["value"]
        return {"hits": {"total": {"value": total_val}, "hits": []}}


def _make_stub() -> _StubFetcher:
    return _StubFetcher((FIX / "gb_nsm_tesco.json").read_text())


# ---------------------------------------------------------------------------
# test_discover_by_lei_parses_hits
# ---------------------------------------------------------------------------

def test_discover_by_lei_parses_hits():
    """discover() returns Documents for every fixture hit that has a download_link."""
    stub = _make_stub()
    src = NsmGB(fetcher=stub)

    docs = src.discover(TESCO_ENTITY)

    assert docs, "expected at least one Document from fixture"
    assert all(d.doc_type in DOC_TYPES for d in docs), "every doc_type must be in DOC_TYPES"
    assert all(
        f["url"].startswith("https://data.fca.org.uk/artefacts/")
        for d in docs
        for f in d.files
    ), "every file URL must start with the artefacts base"
    assert all(d.source == "oam-gb" for d in docs)
    assert all(d.language == "en" for d in docs)
    assert all(d.lei == TESCO_LEI for d in docs)
    assert all(d.country == "GB" for d in docs), "GB entity -> GB-labelled docs"
    assert all(d.files for d in docs), "every Document must have at least one file"


def test_irish_issuer_labelled_with_its_own_country():
    """NSM is the de-facto OAM for Irish issuers; a doc for an IE entity is
    labelled country=IE (issuer jurisdiction) while source stays oam-gb."""
    src = NsmGB(fetcher=_make_stub())
    docs = src.discover(Entity(lei=TESCO_LEI, name="Glanbia plc", country="IE"))
    assert docs
    assert all(d.country == "IE" for d in docs)
    assert all(d.source == "oam-gb" for d in docs)  # provenance preserved


def test_unknown_issuer_country_falls_back_to_source_jurisdiction():
    """A LEI-bearing entity with an unknown country is labelled with the
    mechanism's own jurisdiction (provenance) — not a fabricated third country."""
    src = NsmGB(fetcher=_make_stub())
    docs = src.discover(Entity(lei=TESCO_LEI, name="X", country=""))
    assert docs
    assert all(d.country == NsmGB.country for d in docs)  # == "GB" (source), honest fallback


def test_ireland_wired_to_nsm():
    from company_corpus.eu.acquire import COUNTRY_BACKENDS
    assert COUNTRY_BACKENDS["IE"] is NsmGB


# ---------------------------------------------------------------------------
# test_doc_type_mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("type_str,expected", [
    ("Annual Financial Report", "annual_report"),
    ("Total Voting Rights", "holding_notification"),
    ("Result of AGM", "governance"),
    ("Transaction in Own Shares", "other"),
    ("Interim Management Statement", "interim_statement"),
    ("Prospectus (Equity)", "prospectus"),
    ("Director/PDMR Shareholding", "holding_notification"),
    ("Half-yearly Report", "half_year_report"),
    ("Interim Results", "half_year_report"),
    ("Notice of AGM", "governance"),
])
def test_doc_type_mapping(type_str, expected):
    assert _doc_type(type_str) == expected, (
        f"_doc_type({type_str!r}) expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# test_no_lei_returns_empty
# ---------------------------------------------------------------------------

def test_no_lei_returns_empty():
    """Entity with no LEI must return [] immediately — no fuzzy fallback."""
    stub = _make_stub()
    src = NsmGB(fetcher=stub)

    docs = src.discover(Entity(lei=None, name="X", country="GB"))

    assert docs == []
    assert stub.calls == [], "no HTTP call should be made when LEI is absent"


# ---------------------------------------------------------------------------
# test_post_body_filters_by_lei
# ---------------------------------------------------------------------------

def test_post_body_filters_by_lei():
    """The POST body must filter by the entity's exact LEI."""
    stub = _make_stub()
    src = NsmGB(fetcher=stub)

    src.discover(TESCO_ENTITY)

    assert stub.calls, "expected at least one POST"
    first_body = stub.calls[0]["body"]
    criteria = first_body["criteriaObj"]["criteria"]
    assert criteria == [{"name": "lei", "value": TESCO_LEI}], (
        f"criteria must be exact LEI filter; got {criteria}"
    )
    # Guard the rest of the envelope: a wrong dateCriteria/sort would silently return
    # ALL ~5.3M disclosures (in a different order) instead of the issuer's filings.
    assert first_body["criteriaObj"]["dateCriteria"] is None
    assert first_body["sort"] == "publication_date"
    assert first_body["sortorder"] == "desc"


# ---------------------------------------------------------------------------
# test_pagination_and_truncation
# ---------------------------------------------------------------------------

def test_pagination_terminates_on_empty_page():
    """Pagination stops when the API returns an empty hits list."""

    class _PagingStub:
        def __init__(self, total, pages_with_data):
            self._total = total
            self._pages_with_data = pages_with_data
            self.call_count = 0

        def post_json(self, url, body, **_):
            self.call_count += 1
            page_num = self.call_count  # 1-based
            hits = []
            if page_num <= self._pages_with_data:
                hits = [{
                    "_source": {
                        "disclosure_id": f"id-{page_num}-{i}",
                        "type": "Annual Financial Report",
                        "publication_date": "2025-01-01T00:00:00Z",
                        "download_link": f"NSM/RNS/doc-{page_num}-{i}.pdf",
                        "tag_esef": "",
                        "headline": "AR",
                        "source": "RNS",
                        "isin": "",
                        "company": "TEST CO",
                    }
                } for i in range(_PAGE)]
            return {"hits": {"total": {"value": self._total}, "hits": hits}}

    # 2 full pages then empty → should stop after 3 calls (2 data + 1 empty)
    stub = _PagingStub(total=250, pages_with_data=2)
    src = NsmGB(fetcher=stub)
    docs = src.discover(Entity(lei="TESTLEI", name="Test", country="GB"))
    assert len(docs) == _PAGE * 2
    assert not src.errors


def test_truncation_recorded_when_total_exceeds_max():
    """When total > _MAX_RESULTS a 'truncated' error is recorded."""

    class _TruncStub:
        """Reports total > _MAX_RESULTS; returns empty hits to terminate quickly."""
        def __init__(self):
            self.call_count = 0

        def post_json(self, url, body, **_):
            self.call_count += 1
            # First call: one hit page to exercise the code path
            if self.call_count == 1:
                hits = [{
                    "_source": {
                        "disclosure_id": f"id-{i}",
                        "type": "Other",
                        "publication_date": "2025-01-01T00:00:00Z",
                        "download_link": f"NSM/RNS/doc-{i}.html",
                        "tag_esef": "",
                        "headline": "H",
                        "source": "RNS",
                        "isin": "",
                        "company": "BIG CO",
                    }
                } for i in range(_PAGE)]
                return {"hits": {"total": {"value": _MAX_RESULTS + 1}, "hits": hits}}
            # Subsequent: empty hits
            return {"hits": {"total": {"value": _MAX_RESULTS + 1}, "hits": []}}

    stub = _TruncStub()
    src = NsmGB(fetcher=stub)
    src.discover(Entity(lei="BIGCOlei", name="Big Co", country="GB"))

    truncation_errors = [e for e in src.errors if e["context"] == "truncated"]
    assert truncation_errors, "expected a truncated error when total > _MAX_RESULTS"


def test_no_truncation_error_when_total_below_cap():
    """No truncation error when total fits in one page."""
    stub = _make_stub()
    src = NsmGB(fetcher=stub)
    # The fixture total is 1361, which is well within _MAX_RESULTS (10000),
    # so no truncation error should be recorded.
    src.discover(TESCO_ENTITY)

    truncation_errors = [e for e in src.errors if e["context"] == "truncated"]
    assert not truncation_errors, (
        f"unexpected truncation error for total < {_MAX_RESULTS}: {truncation_errors}"
    )


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def test_list_issuers_returns_empty():
    stub = _make_stub()
    src = NsmGB(fetcher=stub)
    assert src.list_issuers() == []


def test_search_error_is_recorded():
    """A POST failure records a 'search' error and returns empty list."""

    class _FailStub:
        def post_json(self, url, body, **_):
            raise ConnectionError("network down")

    src = NsmGB(fetcher=_FailStub())
    docs = src.discover(TESCO_ENTITY)

    assert docs == []
    assert any(e["context"] == "search" for e in src.errors)


def test_unsafe_download_link_is_skipped_and_recorded():
    """A download_link that could escape the artefacts base (scheme / leading slash /
    parent traversal) is skipped and recorded — never turned into an off-host URL."""

    class _BadLinkStub:
        def __init__(self):
            self.calls = 0

        def post_json(self, url, body, **_):
            self.calls += 1
            if self.calls == 1:
                hits = [{"_source": {"disclosure_id": f"id-{i}", "type": "Other",
                                     "publication_date": "2025-01-01T00:00:00Z",
                                     "download_link": link, "tag_esef": "", "source": "RNS",
                                     "headline": "H", "isin": "", "company": "X"}}
                        for i, link in enumerate([
                            "https://evil.example/x.html",   # scheme
                            "/etc/passwd",                   # absolute
                            "NSM/../../secret.html",         # traversal
                            "NSM/RNS/good.html",             # the only valid one
                        ])]
                return {"hits": {"total": {"value": 4}, "hits": hits}}
            return {"hits": {"total": {"value": 4}, "hits": []}}

    stub = _BadLinkStub()
    src = NsmGB(fetcher=stub)
    docs = src.discover(Entity(lei="TESTLEI", name="X", country="GB"))
    assert len(docs) == 1, "only the safe relative link survives"
    assert docs[0].files[0]["url"] == "https://data.fca.org.uk/artefacts/NSM/RNS/good.html"
    assert all(d.files[0]["url"].startswith("https://data.fca.org.uk/artefacts/") for d in docs)
    assert sum(e["context"] == "download-link" for e in src.errors) == 3


def test_doc_type_empty_or_none_is_other():
    assert _doc_type("") == "other"
    assert _doc_type(None) == "other"


# ---------------------------------------------------------------------------
# A-I2 regression: GB pagination without a "total" field
# ---------------------------------------------------------------------------

def test_pagination_without_total_field():
    """Stub returns 2 full pages then empty with NO 'hits.total' field.
    Old code defaulted absent total to 0 → from_offset=100 >= 0 → stopped after
    page 1.  Fix: absent total keeps None → paginate until empty hits page."""

    class _NoTotalStub:
        def __init__(self):
            self.call_count = 0

        def post_json(self, url, body, **_):
            self.call_count += 1
            from_off = body.get("from", 0)
            if from_off < _PAGE * 2:
                hits = [{
                    "_source": {
                        "disclosure_id": f"id-{from_off}-{i}",
                        "type": "Annual Financial Report",
                        "publication_date": "2025-01-01T00:00:00Z",
                        "download_link": f"NSM/RNS/doc-{from_off}-{i}.pdf",
                        "tag_esef": "",
                        "headline": "AR",
                        "source": "RNS",
                        "isin": "",
                        "company": "TEST CO",
                    }
                } for i in range(_PAGE)]
                # NO 'hits.total' key
                return {"hits": {"hits": hits}}
            # Empty page → terminates
            return {"hits": {"hits": []}}

    stub = _NoTotalStub()
    src = NsmGB(fetcher=stub)
    docs = src.discover(Entity(lei="TESTLEI", name="Test", country="GB"))
    assert len(docs) == _PAGE * 2, (
        f"expected {_PAGE * 2} docs (2 pages × {_PAGE}), got {len(docs)}"
    )
    assert not src.errors
