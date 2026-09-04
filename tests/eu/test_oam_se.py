"""Tests for the Sweden OAM backend (OamSE — Finanscentralen ASP.NET WebForms).

All network-free: a stub fetcher routes get_text (bootstrap) and post_text
(company-name search) calls from real captured fixtures (se_search.html,
se_result_atlascopco.html).

RED -> GREEN discipline: tests were written before the implementation.
"""
from __future__ import annotations

from pathlib import Path

from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_se import (
    OamSE,
    _normalise,
    _scrape_hidden,
    _stockaffect_doc_type,
)

FIX = Path(__file__).parent.parent / "fixtures" / "eu"

_SEARCH_HTML = (FIX / "se_search.html").read_text()
_RESULT_HTML = (FIX / "se_result_atlascopco.html").read_text()

# ---------------------------------------------------------------------------
# Stub fetcher
# ---------------------------------------------------------------------------


class _StubFetcher:
    """Routes fetcher calls by URL:
    * search.aspx GET  -> se_search.html (bootstrap hidden fields)
    * search.aspx POST -> se_result_atlascopco.html (company profile)
    """

    def __init__(
        self,
        *,
        search_html: str = _SEARCH_HTML,
        result_html: str = _RESULT_HTML,
    ):
        self._search = search_html
        self._result = result_html
        self.get_calls: list[str] = []
        self.post_calls: list[str] = []
        self.post_bodies: list[dict] = []

    def get_text(self, url: str, **_) -> str:
        self.get_calls.append(url)
        if "search.aspx" in url:
            return self._search
        raise RuntimeError(f"Unexpected get_text url: {url}")

    def post_text(self, url: str, data=None, **_) -> str:
        # The backend MUST hand the Fetcher a dict so requests sets the
        # form Content-Type; a pre-encoded string would silently drop the
        # search fields (the bug that returned 0 docs live).
        assert isinstance(data, dict), f"post_text body must be a dict, got {type(data)}"
        self.post_calls.append(url)
        self.post_bodies.append(data)
        if "search.aspx" in url:
            return self._result
        raise RuntimeError(f"Unexpected post_text url: {url}")


# ---------------------------------------------------------------------------
# Helper / pure-logic tests
# ---------------------------------------------------------------------------


def test_normalise_strips_swedish_suffix():
    """_normalise strips AB, Aktiebolag and diacritics."""
    assert _normalise("Atlas Copco Aktiebolag") == "atlas copco"
    assert _normalise("Volvo AB") == "volvo"
    assert _normalise("Ericsson AB publ") == "ericsson"
    assert _normalise("  Sandvik  AB  ") == "sandvik"
    # Diacritic stripping: ö -> o
    assert _normalise("Björn AB") == "bjorn"


def test_scrape_hidden_viewstate():
    """_scrape_hidden extracts __VIEWSTATE placeholder from fixture."""
    val = _scrape_hidden(_SEARCH_HTML, '__VIEWSTATE')
    assert val == 'PLACEHOLDER___VIEWSTATE'


def test_scrape_hidden_eventvalidation():
    val = _scrape_hidden(_SEARCH_HTML, '__EVENTVALIDATION')
    assert val == 'PLACEHOLDER___EVENTVALIDATION'


def test_scrape_hidden_missing_field():
    val = _scrape_hidden(_SEARCH_HTML, '__NONEXISTENT')
    assert val == ''


# ---------------------------------------------------------------------------
# Core fixture-driven tests
# ---------------------------------------------------------------------------


def test_discover_returns_documents():
    """discover() for Atlas Copco returns >=1 Document."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    assert docs, "expected at least one Document for Atlas Copco"


def test_discover_all_doc_types_valid():
    """All returned doc_types are in DOC_TYPES."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    assert docs
    for d in docs:
        assert d.doc_type in DOC_TYPES, f"invalid doc_type {d.doc_type!r}"


def test_discover_annual_reports_present():
    """At least one annual_report Document must be present."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    annual = [d for d in docs if d.doc_type == "annual_report"]
    assert annual, "expected at least one annual_report"
    doc = annual[0]
    assert doc.lei == "L1"
    assert doc.country == "SE"
    assert doc.source == "oam-se"


def test_discover_half_year_reports_present():
    """At least one half_year_report Document must be present."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    hy = [d for d in docs if d.doc_type == "half_year_report"]
    assert hy, "expected at least one half_year_report"


def test_discover_interim_statements_present():
    """At least one interim_statement (QuarterReports) must be present."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    interim = [d for d in docs if d.doc_type == "interim_statement"]
    assert interim, "expected at least one interim_statement"


def test_discover_holding_notifications_present():
    """At least one holding_notification (Flaggings) must be present."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    flaggings = [d for d in docs if d.doc_type == "holding_notification"]
    assert flaggings, "expected at least one holding_notification (flaggings)"


def test_discover_financial_report_file_urls_contain_getfile():
    """Annual/half-year/quarterly report Documents have GetFile.aspx?fid= URLs."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    # gvwYearReports, gvwHalfYearReports, gvwQuarterReports, gvwBookEndReports
    # all use GetFile.aspx URLs (identified by native_meta.grid).
    financial_grids = {"gvwYearReports", "gvwHalfYearReports", "gvwQuarterReports", "gvwBookEndReports"}
    financial_docs = [d for d in docs if d.native_meta.get("grid") in financial_grids and d.files]
    assert financial_docs, "expected at least one financial-report doc with files"
    for d in financial_docs:
        for f in d.files:
            assert "GetFile.aspx?fid=" in f["url"], (
                f"expected GetFile.aspx?fid= in URL, got {f['url']!r}"
            )


def test_doc_id_prefixed_se():
    """doc_id must start with 'se-'."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    assert all(d.doc_id.startswith("se-") for d in docs), \
        "all doc_ids must start with 'se-'"


def test_native_meta_carries_grid():
    """native_meta must carry the 'grid' key identifying the source GridView."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    for d in docs:
        assert "grid" in d.native_meta, f"native_meta.grid missing for {d.doc_id}"
    grids_seen = {d.native_meta["grid"] for d in docs}
    # All 6 grids should be represented
    expected = {"gvwYearReports", "gvwHalfYearReports", "gvwQuarterReports",
                "gvwBookEndReports", "gvwFlaggings", "gvStockAffect"}
    assert grids_seen == expected, f"grids_seen={grids_seen!r}"


def test_annual_report_period_extracted():
    """Annual report Documents must have a period in native_meta."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    annual = [d for d in docs if d.doc_type == "annual_report"]
    assert annual
    for d in annual:
        assert d.native_meta.get("period"), \
            f"expected period in native_meta for annual_report {d.doc_id}"


def test_stockaffect_doc_type_mapping():
    """gvStockAffect category text maps to the right DOC_TYPES member."""
    # Insiderinformation -> inside_information
    assert _stockaffect_doc_type("Insiderinformation") == "inside_information"
    assert _stockaffect_doc_type("  insiderinformation  ") == "inside_information"
    # Every other regulated-announcement category -> other
    for cat in ("Hemmedlemsstat", "Förändringar i rättigheter",
                "Ytterligare obligatorisk", "Förvärv av egna aktier", ""):
        assert _stockaffect_doc_type(cat) == "other", cat
    # Result is always a valid DOC_TYPES member.
    for cat in ("Insiderinformation", "Hemmedlemsstat", "anything"):
        assert _stockaffect_doc_type(cat) in DOC_TYPES


def test_stockaffect_inside_information_present():
    """A gvStockAffect row with category 'Insiderinformation' yields an
    inside_information Document carrying a ViewStockAffect.aspx?id= file URL."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    sa = [d for d in docs if d.native_meta.get("grid") == "gvStockAffect"]
    assert sa, "expected gvStockAffect Documents"
    # The fixture includes an Insiderinformation row -> inside_information doc_type.
    ii = [d for d in sa if d.doc_type == "inside_information"]
    assert ii, "expected at least one inside_information doc from gvStockAffect"


def test_stockaffect_rows_carry_viewstockaffect_file_url():
    """Each gvStockAffect Document exposes a downloadable file whose URL points to
    ViewStockAffect.aspx?id= (which redirects to GetFile at download time)."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    sa = [d for d in docs if d.native_meta.get("grid") == "gvStockAffect"]
    assert sa, "expected gvStockAffect Documents"
    for d in sa:
        assert d.files, f"gvStockAffect doc {d.doc_id} must carry a file"
        assert all("ViewStockAffect.aspx?id=" in f["url"] for f in d.files), (
            f"gvStockAffect file URL must be ViewStockAffect.aspx?id=, got {d.files}"
        )


def test_flaggings_are_index_only():
    """Flaggings rows are index-only (no downloadable PDF) -> empty files list."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    flaggings = [d for d in docs if d.native_meta.get("grid") == "gvwFlaggings"]
    assert flaggings
    for d in flaggings:
        assert d.files == [], f"flaggings must be index-only, got {d.files}"


def test_flagging_doc_has_flagging_id():
    """Flagging Documents must carry flagging_id in native_meta."""
    src = OamSE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    flaggings = [d for d in docs if d.doc_type == "holding_notification"]
    assert flaggings
    for d in flaggings:
        assert d.native_meta.get("flagging_id"), \
            f"expected flagging_id in native_meta for holding_notification {d.doc_id}"


def test_empty_name_returns_empty():
    """Entity with no name must return [] without hitting the network."""
    fetcher = _StubFetcher()
    src = OamSE(fetcher=fetcher)
    docs = src.discover(Entity(lei="L1", name="", country="SE"))
    assert docs == []
    assert fetcher.get_calls == [], "no GET calls expected for empty name"
    assert fetcher.post_calls == [], "no POST calls expected for empty name"


def test_bootstrap_failure_returns_empty_and_records_error():
    """If the bootstrap GET fails, discover returns [] and records the error."""

    class _FailBootstrapFetcher:
        def get_text(self, url: str, **_) -> str:
            raise ConnectionError("search page unreachable")

        def post_text(self, url: str, data=None, **_) -> str:
            raise RuntimeError("should not be called")

    src = OamSE(fetcher=_FailBootstrapFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    assert docs == []
    assert any(e["context"] == "bootstrap" for e in src.errors), \
        "bootstrap error must be recorded"


def test_search_failure_returns_empty_and_records_error():
    """If the search POST fails, discover returns [] and records the error."""

    class _FailSearchFetcher:
        def get_text(self, url: str, **_) -> str:
            return _SEARCH_HTML

        def post_text(self, url: str, data=None, **_) -> str:
            raise ConnectionError("search POST failed")

    src = OamSE(fetcher=_FailSearchFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    assert docs == []
    assert any(e["context"] == "search" for e in src.errors), \
        "search error must be recorded"


def test_no_profile_page_records_error():
    """If the POST response is not a company-profile page, an error is recorded."""

    class _WrongPageFetcher:
        def get_text(self, url: str, **_) -> str:
            return _SEARCH_HTML

        def post_text(self, url: str, data=None, **_) -> str:
            return "<html><body>No results found</body></html>"

    src = OamSE(fetcher=_WrongPageFetcher())
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    assert docs == []
    assert any(e["context"] == "search" for e in src.errors), \
        "search error must be recorded for non-profile page"


def test_search_body_is_dict_carrying_company_name():
    """The name search must POST a dict with the entity name in the company-name
    field (a pre-encoded string would silently drop the fields → 0 docs live)."""
    f = _StubFetcher()
    src = OamSE(fetcher=f)
    src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))
    assert f.post_bodies, "the name search must POST a body"
    body = f.post_bodies[0]
    assert isinstance(body, dict)
    assert body["ctl00$main$txtCompanyName"] == "Atlas Copco"


def test_name_mismatch_returns_empty_and_records_error():
    """The form silently returns the closest company; if the profile name does not
    match the requested entity, refuse to bind it (no-guess identity)."""
    # Ask for "Nordea" but the profile page is Atlas Copco -> reject.
    f = _StubFetcher()
    src = OamSE(fetcher=f)
    docs = src.discover(Entity(lei="L1", name="Nordea", country="SE"))
    assert docs == []
    assert any(e["context"] == "name-mismatch" for e in src.errors), \
        "a name-mismatch error must be recorded when the wrong company is returned"


# ---------------------------------------------------------------------------
# Pagination (ViewCompany2.aspx + Page$Next)
# ---------------------------------------------------------------------------

# A second page of gvwYearReports with one extra report row, reached by POSTing
# Page$Next to ViewCompany2.aspx. Only this grid keeps paging; every other grid
# is empty on page 2 so its pagination stops. The pager on this page has no
# further next link, so paging stops after it.
_YEAR_PAGE2_HTML = """
<!DOCTYPE html>
<html><body>
<form method="post" action="ViewCompany2.aspx">
  <input type="hidden" name="__VIEWSTATE" value="VS_PAGE2" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="VSG_PAGE2" />
  <input type="hidden" name="__EVENTVALIDATION" value="EV_PAGE2" />
  <table id="ctl00_main_gvwYearReports">
    <tr><th>Period</th><th>Fil</th></tr>
    <tr><td>2010</td><td><a href='/search/GetFile.aspx?fid=999999'>Svenska</a></td></tr>
  </table>
</form>
</body></html>
"""


def _page2_for(target_grid: str) -> str:
    """Return a page-2 fixture only for `target_grid`; empty for the rest so that
    grid's pagination terminates immediately."""
    if target_grid == "gvwYearReports":
        return _YEAR_PAGE2_HTML
    return _NO_MORE_ROWS_HTML


# A page with the grid table present but no data rows (and no Page$Next pager):
# pagination stops here.
_NO_MORE_ROWS_HTML = """
<!DOCTYPE html>
<html><body>
<form method="post" action="ViewCompany2.aspx">
  <input type="hidden" name="__VIEWSTATE" value="VS_END" />
  <input type="hidden" name="__EVENTVALIDATION" value="EV_END" />
</form>
</body></html>
"""


class _PaginatingFetcher:
    """GET -> search form; first POST to search.aspx -> the profile (page 1).
    Subsequent POSTs to ViewCompany2.aspx with Page$Next -> one extra page for the
    grid named in __EVENTTARGET, then stop."""

    def __init__(self, *, page1_html: str):
        self._search = _SEARCH_HTML
        self._page1 = page1_html
        self.viewcompany_posts: list[dict] = []

    def get_text(self, url, **_):
        return self._search

    def post_text(self, url, data=None, **_):
        # The backend MUST pass a dict (so requests form-encodes it); a string
        # body would silently drop the postback fields.
        assert isinstance(data, dict), f"post_text body must be a dict, got {type(data)}"
        if "ViewCompany2.aspx" in url:
            fields = dict(data)
            self.viewcompany_posts.append(fields)
            target = fields.get("__EVENTTARGET", "")
            grid = target.rsplit("$", 1)[-1] if target else ""
            return _page2_for(grid)
        return self._page1


def test_pagination_fetches_next_page_rows():
    """When the first page advertises a Page$Next pager, discover pages the grid via
    ViewCompany2.aspx and includes the extra page's rows."""
    # Inject a Page$Next pager into the gvwYearReports table on the profile page so
    # the backend knows to page it.
    page1 = _RESULT_HTML.replace(
        'id="ctl00_main_gvwYearReports"',
        'id="ctl00_main_gvwYearReports"',
    )
    # Add a pager postback row into the year-reports table (just before </table>).
    pager_row = (
        '<tr><td><a href="javascript:__doPostBack(&#39;ctl00$main$gvwYearReports&#39;,'
        '&#39;Page$Next&#39;)">&gt;</a></td></tr>'
    )
    # Insert the pager just before the first </table> that closes the year-reports grid.
    import re as _re
    m = _re.search(r'(id="ctl00_main_gvwYearReports".*?)(</table>)', page1, _re.S)
    assert m, "year-reports table must exist in the fixture"
    page1 = page1[:m.end(1)] + pager_row + page1[m.end(1):]

    fetcher = _PaginatingFetcher(page1_html=page1)
    src = OamSE(fetcher=fetcher)
    docs = src.discover(Entity(lei="L1", name="Atlas Copco", country="SE"))

    # The page-2 row carries fid=999999 — it must appear in the results.
    fids = {f.get("fid") for d in docs for f in d.files}
    assert "999999" in fids, "expected the page-2 report (fid=999999) to be discovered"
    # And we actually POSTed a Page$Next to ViewCompany2.aspx for the year grid.
    assert fetcher.viewcompany_posts, "expected at least one ViewCompany2.aspx page POST"
    assert any(
        p.get("__EVENTARGUMENT") == "Page$Next"
        and p.get("__EVENTTARGET") == "ctl00$main$gvwYearReports"
        for p in fetcher.viewcompany_posts
    ), f"expected a Page$Next POST for gvwYearReports; got {fetcher.viewcompany_posts}"


def test_list_issuers_returns_empty():
    assert OamSE(fetcher=_StubFetcher()).list_issuers() == []


# ---------------------------------------------------------------------------
# acquire.py wiring
# ---------------------------------------------------------------------------


def test_country_backends_includes_se():
    from company_corpus.eu.acquire import COUNTRY_BACKENDS
    assert "SE" in COUNTRY_BACKENDS
    assert COUNTRY_BACKENDS["SE"] is OamSE
