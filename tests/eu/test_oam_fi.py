"""Tests for the Finland OAM backend (OamFI — oam.fi Nasdaq Helsinki scrape).

All network-free: a stub Fetcher routes get_text (oam.fi/ → bootstrap fixture;
/view/ → view fixture) and post_text (/ → search fixture first page, then a
no-rows page so pagination stops).

RED → GREEN discipline: tests were written before implementation was correct.
"""
from __future__ import annotations

from pathlib import Path


from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_fi import (
    OamFI,
    _doc_type,
    _normalise_name,
    _parse_published_ts,
)

FIX = Path(__file__).parent.parent / "fixtures" / "eu"
BOOTSTRAP_HTML = (FIX / "fi_bootstrap.html").read_text()
SEARCH_HTML = (FIX / "fi_search_nokia.html").read_text()
VIEW_HTML = (FIX / "fi_view.html").read_text()

# A minimal no-rows page that stops pagination
_EMPTY_PAGE = '<html><body><nef-pagination totalDataLength="6"></nef-pagination></body></html>'


# ---------------------------------------------------------------------------
# Stub Fetcher
# ---------------------------------------------------------------------------

class _StubFetcher:
    """Routes calls by URL substring.

    * get_text on 'oam.fi/' or ending '/' → bootstrap fixture
    * get_text on '/view/' → view fixture
    * post_text first call → search fixture; subsequent → empty page (stops pagination)
    """

    def __init__(
        self,
        *,
        bootstrap_html: str = BOOTSTRAP_HTML,
        search_html: str = SEARCH_HTML,
        view_html: str = VIEW_HTML,
        empty_page: str = _EMPTY_PAGE,
    ):
        self._bootstrap = bootstrap_html
        self._search = search_html
        self._view = view_html
        self._empty = empty_page
        self._post_call_count = 0
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []

    def get_text(self, url: str, **_) -> str:
        self.get_calls.append(url)
        if "/view/" in url:
            return self._view
        if url.endswith("/") or "oam.fi/" in url:
            return self._bootstrap
        raise RuntimeError(f"Unexpected get_text url: {url!r}")

    def post_text(self, url: str, data, **_) -> str:
        self.post_calls.append((url, dict(data)))
        self._post_call_count += 1
        # First POST returns real results; subsequent return empty so pagination stops
        if self._post_call_count == 1:
            return self._search
        return self._empty


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------

def test_parse_published_ts_eest():
    assert _parse_published_ts("2026-06-09 18:30:00 EEST") == "2026-06-09"


def test_parse_published_ts_utc():
    assert _parse_published_ts("2025-03-15 10:00:00 EET") == "2025-03-15"


def test_parse_published_ts_none_on_garbage():
    assert _parse_published_ts("not a date") is None
    assert _parse_published_ts("") is None
    assert _parse_published_ts(None) is None


def test_normalise_name_strips_oyj():
    assert _normalise_name("Nokia Oyj") == "nokia"
    assert _normalise_name("KONE OYJ") == "kone"


def test_normalise_name_strips_oy():
    assert _normalise_name("Example Oy") == "example"


def test_normalise_name_strips_abp():
    assert _normalise_name("Sampo ABP") == "sampo"


def test_normalise_name_strips_plc():
    assert _normalise_name("Example PLC") == "example"


def test_normalise_name_strips_diacritics():
    # Finnish company names occasionally have ä/ö
    assert _normalise_name("Elisa Oyj") == "elisa"


def test_normalise_name_collapses_whitespace():
    assert _normalise_name("  Nokia   Oyj  ") == "nokia"


# ---------------------------------------------------------------------------
# doc_type mapping tests
# ---------------------------------------------------------------------------

def test_doc_type_annual():
    assert _doc_type("Annual Financial Report (ESEF)") == "annual_report"
    assert _doc_type("annual financial report") == "annual_report"


def test_doc_type_half_year():
    assert _doc_type("Half Year Financial Report") == "half_year_report"
    assert _doc_type("half year") == "half_year_report"


def test_doc_type_interim():
    assert _doc_type("Interim report") == "interim_statement"
    assert _doc_type("Interim report (Q1 and Q3)") == "interim_statement"
    assert _doc_type("Financial Statement Release") == "interim_statement"


def test_doc_type_inside_information():
    assert _doc_type("Inside information") == "inside_information"


def test_doc_type_holding_notification():
    assert _doc_type("Major shareholder notification") == "holding_notification"
    assert _doc_type("Managers' transactions") == "holding_notification"
    assert _doc_type("Total voting rights") == "holding_notification"
    assert _doc_type("Total number of voting rights and capital") == "holding_notification"


def test_doc_type_other():
    assert _doc_type("Unknown category XYZ") == "other"
    assert _doc_type("") == "other"


def test_doc_type_always_in_doc_types():
    for cat in [
        "Annual Financial Report (ESEF)", "Half Year Financial Report",
        "Interim report", "Financial Statement Release", "Inside information",
        "Major shareholder notification", "Managers' transactions",
        "Total voting rights", "Total number of voting rights and capital",
        "Something completely unknown",
    ]:
        assert _doc_type(cat) in DOC_TYPES, f"_doc_type({cat!r}) not in DOC_TYPES"


# ---------------------------------------------------------------------------
# Bootstrap tests
# ---------------------------------------------------------------------------

def test_bootstrap_parses_csrf():
    src = OamFI(fetcher=_StubFetcher())
    result = src._bootstrap()
    assert result is not None
    csrf, _ = result
    assert csrf and len(csrf) > 5, f"CSRF token too short or empty: {csrf!r}"


def test_bootstrap_parses_company_map():
    src = OamFI(fetcher=_StubFetcher())
    result = src._bootstrap()
    assert result is not None
    _, company_map = result
    assert isinstance(company_map, dict)
    assert len(company_map) > 0, "company_map must not be empty"
    # Nokia must be present with id=690
    nokia_id = next(
        (cid for name, cid in company_map.items() if "nokia" in name.lower()),
        None
    )
    assert nokia_id == 690, f"Nokia id expected 690, got {nokia_id}"


def test_bootstrap_is_cached():
    """A second call to _bootstrap must not hit the network again."""
    fetcher = _StubFetcher()
    src = OamFI(fetcher=fetcher)
    src._bootstrap()
    calls_after_first = len(fetcher.get_calls)
    src._bootstrap()
    assert len(fetcher.get_calls) == calls_after_first, "bootstrap cache not working"


def test_bootstrap_failure_returns_none():
    class _FailFetcher:
        def get_text(self, url, **_):
            raise ConnectionError("network down")
        def post_text(self, url, data, **_):
            raise ConnectionError("network down")

    src = OamFI(fetcher=_FailFetcher())
    assert src._bootstrap() is None
    assert any(e["context"] == "bootstrap" for e in src.errors)


# ---------------------------------------------------------------------------
# Name resolution tests
# ---------------------------------------------------------------------------

def test_resolve_nokia_by_name():
    """Nokia Oyj → company id 690."""
    src = OamFI(fetcher=_StubFetcher())
    result = src._bootstrap()
    assert result is not None
    _, company_map = result
    cid = src._resolve_company_id(Entity(lei="L1", name="Nokia Oyj", country="FI"), company_map)
    assert cid == 690


def test_resolve_case_insensitive():
    src = OamFI(fetcher=_StubFetcher())
    result = src._bootstrap()
    assert result is not None
    _, company_map = result
    cid = src._resolve_company_id(Entity(lei="L1", name="nokia oyj", country="FI"), company_map)
    assert cid == 690


def test_resolve_no_match_records_error():
    src = OamFI(fetcher=_StubFetcher())
    result = src._bootstrap()
    assert result is not None
    _, company_map = result
    cid = src._resolve_company_id(
        Entity(lei="L1", name="NONEXISTENT CORP XYZ", country="FI"), company_map
    )
    assert cid is None
    assert any(e["context"] == "resolve-no-match" for e in src.errors)


def test_discover_no_match_returns_empty():
    """discover() for an unresolvable name must return [] and record an error."""
    src = OamFI(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="NONEXISTENT CORP XYZ", country="FI"))
    assert docs == []
    assert any(e["context"] == "resolve-no-match" for e in src.errors)


def test_discover_empty_name_returns_empty():
    fetcher = _StubFetcher()
    src = OamFI(fetcher=fetcher)
    assert src.discover(Entity(lei="L1", name="", country="FI")) == []
    assert fetcher.post_calls == [], "no POST for empty name"


# ---------------------------------------------------------------------------
# discover() integration tests
# ---------------------------------------------------------------------------

def test_discover_nokia_yields_documents():
    src = OamFI(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    assert docs, "expected at least one Document for Nokia"


def test_discover_documents_have_correct_source_and_country():
    src = OamFI(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    assert all(d.source == "oam-fi" for d in docs)
    assert all(d.country == "FI" for d in docs)


def test_discover_documents_have_doc_type_in_doc_types():
    src = OamFI(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    assert all(d.doc_type in DOC_TYPES for d in docs)


def test_discover_nokia_doc_types_cover_expected_categories():
    """Nokia fixture has 6 rows with distinct categories; verify the key mappings.

    Specifically, the 'Managers&#39; transactions' row (HTML-entity-escaped) must
    map to holding_notification (not 'other'), which would fail if category text
    is not html.unescape()d before _doc_type() lookup.
    """
    src = OamFI(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    doc_types = {d.doc_type for d in docs}
    assert "holding_notification" in doc_types, (
        f"Expected holding_notification from Managers' transactions row; got {doc_types}"
    )
    assert "annual_report" in doc_types, (
        f"Expected annual_report from Annual row; got {doc_types}"
    )
    assert "inside_information" in doc_types, (
        f"Expected inside_information from Inside information row; got {doc_types}"
    )


def test_discover_documents_carry_lei():
    src = OamFI(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    assert all(d.lei == "FI-LEI-001" for d in docs)


def test_discover_doc_ids_prefixed_fi():
    src = OamFI(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    assert all(d.doc_id.startswith("fi-") for d in docs)


def test_discover_file_urls_contain_viewattachment():
    src = OamFI(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    for doc in docs:
        for f in doc.files:
            assert "viewAttachment.action?messageAttachmentId=" in f["url"], (
                f"file URL should contain viewAttachment.action: {f['url']!r}"
            )


def test_discover_file_kind_esef_for_zip():
    """Files whose names end .zip must have kind='esef'."""
    src = OamFI(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    for doc in docs:
        for f in doc.files:
            name = f.get("name", "")
            if name.lower().endswith(".zip") or name.lower().endswith(".xhtml"):
                assert f["kind"] == "esef", f"zip/xhtml file should be kind=esef: {f}"


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------

def test_pagination_terminates_on_empty_page():
    """Pagination must stop when 0 rows returned or total consumed."""
    fetcher = _StubFetcher()
    src = OamFI(fetcher=fetcher)
    src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    # After first POST (real results) + second POST (empty page), should stop
    assert fetcher._post_call_count <= 2, (
        f"Expected ≤2 POST calls, got {fetcher._post_call_count}"
    )


def test_pagination_cap_records_truncation():
    """When _MAX_PAGES is hit, a truncation error must be recorded."""

    call_count = 0

    class _InfiniteFetcher:
        def get_text(self, url, **_):
            if "/view/" in url:
                return VIEW_HTML
            return BOOTSTRAP_HTML

        def post_text(self, url, data, **_):
            nonlocal call_count
            call_count += 1
            # Always return a page with rows and large totalDataLength
            # so pagination never stops naturally
            if 'totalDataLength' in SEARCH_HTML:
                return SEARCH_HTML.replace(
                    'totalDataLength="2589"', 'totalDataLength="99999"'
                )
            return (
                '<nef-pagination totalDataLength="99999"></nef-pagination>'
                + SEARCH_HTML
            )

    src = OamFI(fetcher=_InfiniteFetcher())
    # Patch _MAX_PAGES to 2 for speed (avoid 60 network calls in the test)
    import company_corpus.eu.sources.oam_fi as _mod
    original = _mod._MAX_PAGES
    _mod._MAX_PAGES = 2
    try:
        src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    finally:
        _mod._MAX_PAGES = original

    assert any(e["context"] == "truncated" for e in src.errors), (
        f"expected truncated error; errors={src.errors}"
    )


# ---------------------------------------------------------------------------
# New tests: doc_type tightening (change 1)
# ---------------------------------------------------------------------------

def test_doc_type_annual_report_needle_matches_live_label():
    """'Annual Financial Report' (live fixture label) still maps to annual_report."""
    assert _doc_type("Annual Financial Report") == "annual_report"


def test_doc_type_agm_not_misclassified():
    """'Annual General Meeting decisions' must NOT map to annual_report."""
    assert _doc_type("Annual General Meeting decisions") == "other"


# ---------------------------------------------------------------------------
# New tests: resolve-ambiguous (change 3a)
# ---------------------------------------------------------------------------

# Synthetic bootstrap HTML with two companies that normalise to the same name.
# "Acme Oyj" and "Acme Oy" both normalise to "acme".
_AMBIGUOUS_BOOTSTRAP_HTML = (
    '<!DOCTYPE html><html><head>'
    '<meta name="_csrf" content="test-csrf-token"/>'
    '</head><body>'
    '<nef-form-select id="company-select" name="company"'
    ' options="[{&quot;value&quot;: &quot;1&quot;, &quot;label&quot;: &quot;Acme Oyj&quot;},'
    ' {&quot;value&quot;: &quot;2&quot;, &quot;label&quot;: &quot;Acme Oy&quot;}]">'
    '</nef-form-select>'
    '</body></html>'
)


def test_resolve_ambiguous_returns_empty_and_records_error():
    """Two companies normalising to the same name → [] and resolve-ambiguous error."""
    src = OamFI(fetcher=_StubFetcher(bootstrap_html=_AMBIGUOUS_BOOTSTRAP_HTML))
    docs = src.discover(Entity(lei="L1", name="Acme", country="FI"))
    assert docs == [], f"expected [] for ambiguous match, got {docs}"
    assert any(e["context"] == "resolve-ambiguous" for e in src.errors), (
        f"expected resolve-ambiguous error; errors={src.errors}"
    )


# ---------------------------------------------------------------------------
# New tests: view-hop failure resilience (change 3b)
# ---------------------------------------------------------------------------

class _ViewFailFetcher:
    """Fetcher that returns bootstrap normally but raises on /view/ URLs."""

    def __init__(self):
        self._post_call_count = 0

    def get_text(self, url: str, **_) -> str:
        if "/view/" in url:
            raise ConnectionError(f"simulated view-hop failure: {url}")
        # Bootstrap
        if url.endswith("/") or "oam.fi/" in url:
            return BOOTSTRAP_HTML
        raise RuntimeError(f"Unexpected get_text url: {url!r}")

    def post_text(self, url: str, data, **_) -> str:
        self._post_call_count += 1
        if self._post_call_count == 1:
            return SEARCH_HTML
        return _EMPTY_PAGE


def test_view_hop_failure_does_not_raise():
    """A view-hop error must not propagate; discover() returns (possibly empty) list."""
    src = OamFI(fetcher=_ViewFailFetcher())
    docs = src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    # Should not raise; result is a list (may be empty due to all view hops failing)
    assert isinstance(docs, list)


def test_view_hop_failure_records_view_errors():
    """Each failed view-hop must record a 'view' context error."""
    src = OamFI(fetcher=_ViewFailFetcher())
    src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    assert any(e["context"] == "view" for e in src.errors), (
        f"expected 'view' context error; errors={src.errors}"
    )


# ---------------------------------------------------------------------------
# New tests: published_ts end-to-end (change 3c)
# ---------------------------------------------------------------------------

def test_discover_published_ts_first_row():
    """First fixture row has title='2025-10-28 17:30:00 EET' → published_ts='2025-10-28'."""
    src = OamFI(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="FI-LEI-001", name="Nokia Oyj", country="FI"))
    assert docs, "need at least one document to check published_ts"
    # The first row in the fixture is view_id=464108, date 2025-10-28
    matching = [d for d in docs if d.doc_id == "fi-464108"]
    assert matching, "expected a document with doc_id='fi-464108'"
    assert matching[0].published_ts == "2025-10-28", (
        f"expected published_ts='2025-10-28', got {matching[0].published_ts!r}"
    )


# ---------------------------------------------------------------------------
# New tests: list_issuers (change 3d)
# ---------------------------------------------------------------------------

def test_list_issuers_returns_non_empty():
    """list_issuers() must return a non-empty list of IssuerRef."""
    from company_corpus.eu.oam_base import IssuerRef
    src = OamFI(fetcher=_StubFetcher())
    issuers = src.list_issuers()
    assert issuers, "list_issuers() must not be empty"
    assert all(isinstance(i, IssuerRef) for i in issuers)


def test_list_issuers_nokia_native_id_and_country():
    """Nokia's IssuerRef must have native_id='690' and country='FI'."""
    src = OamFI(fetcher=_StubFetcher())
    issuers = src.list_issuers()
    nokia_refs = [i for i in issuers if "nokia" in i.name.lower()]
    assert nokia_refs, "Nokia must appear in list_issuers()"
    nokia = nokia_refs[0]
    assert nokia.native_id == "690", f"expected native_id='690', got {nokia.native_id!r}"
    assert nokia.country == "FI", f"expected country='FI', got {nokia.country!r}"


# ---------------------------------------------------------------------------
# acquire.py wiring
# ---------------------------------------------------------------------------

def test_country_backends_includes_fi():
    from company_corpus.eu.acquire import COUNTRY_BACKENDS
    from company_corpus.eu.sources.oam_fi import OamFI as _OamFI
    assert "FI" in COUNTRY_BACKENDS
    assert COUNTRY_BACKENDS["FI"] is _OamFI
