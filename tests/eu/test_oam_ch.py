"""Tests for the Switzerland backend (DisclosureCH — SIX + EQS aggregator).

Network-free: a stub Fetcher serves the captured SIX ``equityissuer.json`` and
the two EQS HTML fixtures (name-search + per-company feed). The stub keys SIX by
the ISIN query param (only the listed equity ISIN returns data) and EQS by the
``action`` param, exactly as the live endpoints do.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_ch import (
    DisclosureCH,
    _dedup_key,
    _doc_type,
    _eqs_query,
    _ts_from_eqs_date,
    _ts_from_millis,
)

FIX = Path(__file__).parent.parent / "fixtures" / "eu"
_SIX_FIX = json.loads((FIX / "ch_equityissuer_abb.json").read_text())
_EQS_SEARCH = (FIX / "ch_eqs_search.html").read_text()
_EQS_FEED = (FIX / "ch_eqs_feed.html").read_text()

_EQUITY_ISIN = "CH0012221716"

ABB = Entity(
    lei="5493000LKVGOO9PELI61",
    name="ABB Ltd",
    country="CH",
    isins=("CH0099999999", _EQUITY_ISIN, "US0003751056"),  # equity line not first
)


class _StubFetcher:
    """SIX feed via get_json (keyed by ISIN); EQS via get_text (keyed by action)."""

    _EMPTY = {"data": [], "total": 0, "limit": 100, "offset": 0}

    def __init__(self, *, six=None, eqs_search=None, eqs_feed=None):
        self._six = six if six is not None else _SIX_FIX
        self._eqs_search = eqs_search if eqs_search is not None else _EQS_SEARCH
        self._eqs_feed = eqs_feed if eqs_feed is not None else _EQS_FEED
        self.eqs_search_queries: list[str] = []

    def get_json(self, url: str, **_):
        q = parse_qs(urlsplit(url).query)
        isin = q.get("isin", [""])[0]
        page = int(q.get("pageNumber", ["0"])[0])
        if isin != _EQUITY_ISIN:
            return dict(self._EMPTY)
        return self._six if page == 0 else dict(self._EMPTY)

    def get_text(self, url: str, *, params=None, **_):
        params = params or {}
        action = params.get("action")
        if action == "fetch_realtime_news_data":
            self.eqs_search_queries.append(params.get("filter[search]"))
            return self._eqs_search
        if action == "fetch_eqs_financial_news_data":
            return self._eqs_feed if int(params.get("pageNo", 1)) == 1 else "<html></html>"
        raise RuntimeError(f"unexpected get_text action: {action}")


# ---------------------------------------------------------------------------
# Pure-logic tests
# ---------------------------------------------------------------------------


def test_doc_type_rules_and_ad_hoc_fallback():
    assert _doc_type("ABB publishes its Annual Reporting Suite 2025", False) == "annual_report"
    assert _doc_type("Half-year report 2026", False) == "half_year_report"
    assert _doc_type("Q3 2025 results", False) == "interim_statement"
    assert _doc_type("ABB raises guidance", True) == "inside_information"
    assert _doc_type("ABB share buybacks", False) == "other"


def test_ts_helpers():
    assert _ts_from_millis(1740960000000) == "2025-03-03T00:00:00+00:00"
    assert _ts_from_millis(None) is None
    assert _ts_from_eqs_date("25 June 2026") == "2026-06-25T00:00:00+00:00"
    assert _ts_from_eqs_date("garbage") is None


def test_eqs_query_strips_legal_suffix():
    assert _eqs_query("LOGITECH INTERNATIONAL S.A.") == "LOGITECH INTERNATIONAL"
    assert _eqs_query("ABB Ltd") == "ABB"
    assert _eqs_query("Partners Group Holding AG") == "Partners Group Holding"


def test_dedup_key_normalises_title_and_day():
    assert _dedup_key("ABB  Ltd: X", "2026-06-25T00:00:00+00:00") == \
        _dedup_key("abb ltd: x", "2026-06-25T09:30:00+00:00")


# ---------------------------------------------------------------------------
# discover() — aggregation
# ---------------------------------------------------------------------------


def test_discover_unions_six_and_eqs():
    """SIX (3 items) + EQS (2 cards, one a dup of a SIX item) -> 4 unique docs."""
    src = DisclosureCH(fetcher=_StubFetcher())
    docs = src.discover(ABB)
    prov = [d.native_meta["provider"] for d in docs]
    assert prov.count("six") == 3
    assert prov.count("eqs") == 1   # the dup collapsed; the unique one survives
    assert all(d.country == "CH" and d.source == "oam-ch" for d in docs)
    assert all(d.doc_type in DOC_TYPES for d in docs)
    assert not src.errors


def test_cross_provider_dedup_drops_the_overlap():
    """The EQS card repeating a SIX announcement (same title+day) is not re-added."""
    src = DisclosureCH(fetcher=_StubFetcher())
    docs = src.discover(ABB)
    # The buyback announcement exists in both feeds; it must appear exactly once,
    # and from SIX (richer metadata wins).
    buyback = [d for d in docs
               if "share buybacks - june 18" in d.native_meta["title"].lower()]
    assert len(buyback) == 1
    assert buyback[0].native_meta["provider"] == "six"


def test_eqs_only_item_is_added_with_article_url():
    src = DisclosureCH(fetcher=_StubFetcher())
    docs = src.discover(ABB)
    hy = next(d for d in docs if d.native_meta["provider"] == "eqs")
    assert hy.doc_type == "half_year_report"
    assert hy.published_ts == "2026-05-10T00:00:00+00:00"
    assert hy.files == [{"name": "announcement.html", "kind": "announcement",
                         "url": "https://www.eqs-news.com/news/category/abb-uniq/uniq1_en"}]


def test_eqs_search_uses_simplified_query():
    f = _StubFetcher()
    DisclosureCH(fetcher=f).discover(ABB)
    assert f.eqs_search_queries == ["ABB"]  # "ABB Ltd" -> "ABB"


def test_eqs_isin_verification_rejects_wrong_company():
    """A search whose only card carries a foreign ISIN yields no EQS docs (no-guess)."""
    search_only_other = _EQS_SEARCH.replace('data-news-isin="CH0012221716"',
                                            'data-news-isin="DE000WRONG999"')
    f = _StubFetcher(eqs_search=search_only_other)
    docs = DisclosureCH(fetcher=f).discover(ABB)
    assert all(d.native_meta["provider"] == "six" for d in docs)  # EQS contributed nothing


def test_six_attachments_and_inline_html_preserved():
    docs = DisclosureCH(fetcher=_StubFetcher()).discover(ABB)
    buyback = next(d for d in docs if d.native_meta.get("six_id") == "8764")
    kinds = [x["kind"] for x in buyback.files]
    assert kinds.count("announcement") == 1 and kinds.count("document") == 2


def test_no_isin_records_error_and_returns_empty():
    src = DisclosureCH(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="No ISIN Co", country="CH", isins=()))
    assert docs == []
    assert any(e["context"] == "no-isin" for e in src.errors)


def test_empty_name_skips_eqs_only():
    """With no name, EQS cannot be searched; SIX still runs by ISIN."""
    src = DisclosureCH(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L", name="", country="CH", isins=(_EQUITY_ISIN,)))
    assert docs and all(d.native_meta["provider"] == "six" for d in docs)


def test_country_backends_includes_ch():
    from company_corpus.eu.acquire import COUNTRY_BACKENDS
    assert COUNTRY_BACKENDS["CH"] is DisclosureCH


# ---------------------------------------------------------------------------
# A-I2 regression: SIX pagination without a "total" field
# ---------------------------------------------------------------------------

def test_six_pagination_without_total_field():
    """SIX feed returns 2 full pages then empty with NO 'total' field.
    Old code defaulted absent total to 0 → ``len(items) >= 0`` always True → stopped
    after page 1.  Fix: absent total keeps None → paginate until empty page."""
    from urllib.parse import parse_qs, urlsplit

    _PAGE = 100

    def _six_item(i):
        return {
            "id": str(i),
            "ad_hoc": False,
            "news_date": 1740960000000,
            "content": [{"title": f"Report {i}", "language": "en", "content": f"<p>Body {i}</p>"}],
            "company": {"name": "TEST CO"},
        }

    page0 = [_six_item(i) for i in range(_PAGE)]
    page1 = [_six_item(_PAGE + i) for i in range(_PAGE)]

    class _NoTotalFetcher:
        """SIX feed: 2 full pages then empty, NO 'total' key.  EQS: always empty."""

        def get_json(self, url: str, **_):
            q = parse_qs(urlsplit(url).query)
            isin = q.get("isin", [""])[0]
            if isin != _EQUITY_ISIN:
                return {"data": []}              # unknown ISIN → empty
            page = int(q.get("pageNumber", ["0"])[0])
            if page == 0:
                return {"data": page0}           # NO 'total' key
            if page == 1:
                return {"data": page1}           # NO 'total' key
            return {"data": []}                  # empty → terminates

        def get_text(self, url: str, *, params=None, **_):
            return "<html></html>"               # EQS always empty

    src = DisclosureCH(fetcher=_NoTotalFetcher())
    docs = src.discover(ABB)
    six_docs = [d for d in docs if d.native_meta["provider"] == "six"]
    assert len(six_docs) == _PAGE * 2, (
        f"expected {_PAGE * 2} SIX docs (2 pages × {_PAGE}), got {len(six_docs)}"
    )
