"""Tests for the Euronext backend (EuronextSource — cross-market notices feed).

Network-free: a stub Fetcher serves the captured notices fixture keyed by the
``<ISIN>-<MIC>`` path segment, exactly as the live per-issuer feed is keyed.
"""
from __future__ import annotations

from pathlib import Path

from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_euronext import (
    EURONEXT_MICS,
    EuronextSource,
    _doc_type,
    _published_ts,
)

FIX = Path(__file__).parent.parent / "fixtures" / "eu"
_NOTICES = (FIX / "euronext_notices_edp.html").read_text()

_EDP_ISIN = "PTEDP0AM0009"
EDP = Entity(lei="LEDP", name="EDP", country="PT", isins=(_EDP_ISIN,))

_EMPTY = '<div class="card"><h3>NOTICES</h3></div>'


class _StubFetcher:
    """Serves the notices fixture for the EDP/XLIS path, empty otherwise."""

    def __init__(self, *, notices=None):
        self._notices = notices if notices is not None else _NOTICES
        self.urls: list[str] = []

    def get_text(self, url: str, **_):
        self.urls.append(url)
        if f"{_EDP_ISIN}-XLIS" in url:
            return self._notices
        return _EMPTY


# ---------------------------------------------------------------------------
# Pure-logic tests
# ---------------------------------------------------------------------------


def test_doc_type_mapping():
    assert _doc_type("CE - Shares - Dividend - Announcement") == "other"
    assert _doc_type("Notice of General Meeting") == "governance"
    assert _doc_type("CE - Change of issuer/product name") == "governance"
    assert _doc_type("Annual Financial Report 2025") == "annual_report"
    assert _doc_type("Prospectus approved") == "prospectus"


def test_published_ts():
    assert _published_ts("23 Apr 2026") == "2026-04-23T00:00:00+00:00"
    assert _published_ts("") is None
    assert _published_ts("not a date") is None


def _instr_row(nid, instrument, *, with_pdf=False):
    pdf = ('<td><a href="/en/listview/notice-download?id=1&amp;type=PDF&amp;attachmentId=2">PDF</a></td>'
           if with_pdf else "<td></td>")
    return (
        f'<tr class="row_{nid} ">'
        f'<td class="noticenumber">OSL_{nid}</td>'
        f'<td class="noticedate priority-low">23 Apr 2026</td><td class="effect"></td>'
        f'<td class="noticename notice-abstract-load">CE - Dividend</td>'
        f'<td class="instruments notice-detail-load"><span><div class="d-flex">'
        f'<button class="btn"><span class="sr-only">Toggle Visibility</span></button>'
        f'{instrument}</div></span></td>{pdf}</tr>'
    )


class _ListingStub:
    def __init__(self, html):
        self._html = html
        self.urls = []

    def get_text(self, url, **_):
        import re
        self.urls.append(url)
        pg = int(re.search(r"pageNum=(\d+)", url).group(1))
        return self._html if pg == 1 else "<table><tbody></tbody></table>"


def test_listing_mode_verifies_issuer_and_drops_market_noise():
    """force_mic = listing mode: keep only notices whose issuer cell matches the
    entity; the feed's market-wide 'Multiple' and a wrong issuer are rejected."""
    feed = ("<table><tbody>"
            + _instr_row("1", "2020 BULKERS", with_pdf=True)
            + _instr_row("2", "Multiple")
            + _instr_row("3", "SOME OTHER CO")
            + "</tbody></table>")
    e = Entity(lei="L", name="2020 BULKERS LTD.", country="BM", isins=("BMG9156K1018",))
    stub = _ListingStub(feed)
    docs = EuronextSource(fetcher=stub, force_mic="XPAR").discover(e)
    assert len(docs) == 1
    assert docs[0].native_meta["notice_number"] == "OSL_1"
    assert docs[0].country == "BM"  # issuer jurisdiction, listing-sourced
    # query went out under the listing MIC, keyed by the entity's ISIN
    assert "BMG9156K1018-XPAR" in stub.urls[0]


def test_listing_mode_no_match_yields_nothing():
    feed = "<table><tbody>" + _instr_row("9", "Multiple") + "</tbody></table>"
    e = Entity(lei="L", name="2020 Bulkers Ltd", country="BM", isins=("BMG9156K1018",))
    docs = EuronextSource(fetcher=_ListingStub(feed), force_mic="XPAR").discover(e)
    assert docs == []


def test_country_to_mic_map_covers_euronext_markets():
    # Ireland is excluded: Euronext Dublin's per-issuer feed is empty, so Irish
    # issuers resolve through the FCA NSM (GB backend) by LEI instead.
    assert set(EURONEXT_MICS) == {"NL", "BE", "FR", "PT", "NO"}
    assert EURONEXT_MICS["PT"] == "XLIS"
    assert "IE" not in EURONEXT_MICS


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


def test_discover_parses_notices():
    src = EuronextSource(fetcher=_StubFetcher())
    docs = src.discover(EDP)
    assert len(docs) == 3
    assert all(d.country == "PT" and d.source == "euronext" for d in docs)
    assert all(d.doc_type in DOC_TYPES for d in docs)
    assert {d.doc_type for d in docs} == {"other", "governance"}
    assert not src.errors


def test_attachment_becomes_a_file_else_index_only():
    docs = EuronextSource(fetcher=_StubFetcher()).discover(EDP)
    by_num = {d.native_meta["notice_number"]: d for d in docs}
    # Dividend notice carries a PDF download.
    div = by_num["LIS_20260423_00070_EUR"]
    # The title is clean (the collapse button's "Toggle Visibility" label stripped).
    assert div.native_meta["title"] == "CE - Shares - Dividend - Announcement"
    assert div.files and div.files[0]["kind"] == "document"
    assert div.files[0]["url"].endswith("id=1544726&type=PDF&attachmentId=426888")
    assert div.published_ts == "2026-04-23T00:00:00+00:00"
    # Name-change notice has no attachment -> index-only (recorded, no file).
    name = by_num["LIS_20250312_00041_EUR"]
    assert name.files == [] and name.native_meta["has_attachment"] is False


def test_mic_resolved_from_country_and_query_path():
    f = _StubFetcher()
    EuronextSource(fetcher=f).discover(EDP)
    # MIC XLIS resolved from country PT; paginated via the feed's own pageNum param.
    assert f.urls[0] == (
        f"https://live.euronext.com/en/ajax/getNoticePublicData/{_EDP_ISIN}-XLIS"
        "?pageSize=50&alias=1&pageNum=1"
    )


def test_non_euronext_country_yields_nothing():
    src = EuronextSource(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L", name="X", country="DE", isins=("DE000A1EWWW0",)))
    assert docs == [] and not src.errors  # backend simply does not apply


def test_no_isin_records_error():
    src = EuronextSource(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L", name="No ISIN", country="PT", isins=()))
    assert docs == []
    assert any(e["context"] == "no-isin" for e in src.errors)


def test_dedup_by_notice_id_across_isins():
    """Two ISINs returning overlapping notices collapse by notice id."""
    class _TwoIsinStub(_StubFetcher):
        def get_text(self, url, **_):
            self.urls.append(url)
            # Both ISINs map to the same notices page.
            return self._notices

    e = Entity(lei="L", name="EDP", country="PT", isins=(_EDP_ISIN, "PTEDP0AM0011"))
    src = EuronextSource(fetcher=_TwoIsinStub())
    docs = src.discover(e)
    assert len(docs) == 3  # not 6 — deduped by notice id


def _rows_html(start: int, n: int) -> str:
    return "".join(
        f'<tr class="row_{i} "><td class="noticenumber">N{i}</td>'
        f'<td class="noticedate priority-low">01 Jan 2026</td>'
        f'<td class="noticename notice-abstract-load">CE - X</td></tr>'
        for i in range(start, start + n)
    )


class _PaginatingStub:
    """Serves distinct pages by the ``pageNum`` query param (last page short)."""

    def __init__(self, page_sizes):
        self._sizes = page_sizes
        self.pages_fetched: list[int] = []

    def get_text(self, url, **_):
        import re
        pg = int(re.search(r"pageNum=(\d+)", url).group(1))
        self.pages_fetched.append(pg)
        if pg > len(self._sizes):
            return "<table><tbody></tbody></table>"
        start = sum(self._sizes[: pg - 1])
        return f"<table><tbody>{_rows_html(start, self._sizes[pg - 1])}</tbody></table>"


def test_paginates_all_pages_to_exhaustivity():
    """A 50+40 history is fully fetched via pageNum, stopping on the short page."""
    f = _PaginatingStub([50, 40])
    src = EuronextSource(fetcher=f)
    docs = src.discover(EDP)
    assert len(docs) == 90               # both pages, no notice lost
    assert f.pages_fetched == [1, 2]     # stops after the short (40<50) page
    assert not src.errors                # exhaustive -> no truncation recorded


def test_nested_markup_row_keeps_its_download_link():
    """A row whose cell contains a nested <table>/<tr> must not be truncated at
    the inner </tr> — its trailing download cell must survive (regression)."""
    nested = (
        '<table><tbody>'
        '<tr class="row_900 ">'
        '  <td class="noticenumber">LIS_X</td>'
        '  <td class="noticedate priority-low">23 Apr 2026</td>'
        '  <td class="noticename notice-abstract-load">CE - Shares - Dividend'
        '    <table><tr><td>nested cell</td></tr></table>'  # nested </tr> here
        '  </td>'
        '  <td><a href="/en/listview/notice-download?id=77&amp;type=PDF&amp;attachmentId=88">PDF</a></td>'
        '</tr>'
        '</tbody></table>'
    )
    src = EuronextSource(fetcher=_StubFetcher(notices=nested))
    docs = src.discover(EDP)
    assert len(docs) == 1
    assert docs[0].files and docs[0].files[0]["url"].endswith("id=77&type=PDF&attachmentId=88")


def test_download_link_param_order_independent():
    """The download link is parsed order-free: type before the ids still works."""
    row = (
        '<table><tbody><tr class="row_901 ">'
        '  <td class="noticenumber">LIS_Y</td>'
        '  <td class="noticedate priority-low">23 Apr 2026</td>'
        '  <td class="noticename">CE - X</td>'
        '  <td><a href="/en/listview/notice-download?type=PDF&amp;attachmentId=88&amp;id=77">PDF</a></td>'
        '</tr></tbody></table>'
    )
    docs = EuronextSource(fetcher=_StubFetcher(notices=row)).discover(EDP)
    assert docs[0].files and docs[0].files[0]["url"].endswith("id=77&type=PDF&attachmentId=88")


def test_unparseable_download_link_records_error():
    """A download link with no extractable ids surfaces a parse error (not silent)."""
    row = (
        '<table><tbody><tr class="row_902 ">'
        '  <td class="noticenumber">LIS_Z</td>'
        '  <td class="noticedate priority-low">23 Apr 2026</td>'
        '  <td class="noticename">CE - X</td>'
        '  <td><a href="/en/listview/notice-download?foo=bar">PDF</a></td>'
        '</tr></tbody></table>'
    )
    src = EuronextSource(fetcher=_StubFetcher(notices=row))
    docs = src.discover(EDP)
    assert docs[0].files == []  # no bogus file built
    assert any(e["context"] == "download-parse" for e in src.errors)
