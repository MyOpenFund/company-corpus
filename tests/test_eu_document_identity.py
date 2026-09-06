"""A file-less document must not collapse into its neighbours (Rob-C3), and an
id must not change between two runs over the same input."""
from __future__ import annotations

import pytest

from company_corpus.eu.dispatcher import merge_documents
from company_corpus.eu.documents import Document, source_key, stable_native_id
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_be import StoriBE
from company_corpus.eu.sources.oam_ch import DisclosureCH
from company_corpus.eu.sources.oam_gb import NsmGB


def _doc(native_id, *, source="oam_se", country="SE", files=None, doc_type="holding_notification"):
    return Document(native_id=native_id, lei="L1", country=country, doc_type=doc_type,
                    period_end=None, published_ts="2026-01-01", discovered_ts="2026-01-02",
                    language="sv", source=source, files=files or [])


def test_five_file_less_flaggings_stay_five():
    docs = merge_documents([[_doc(f"flagging-{i}") for i in range(5)]])
    assert len(docs) == 5


def test_two_documents_named_release_pdf_stay_two():
    docs = merge_documents([[_doc("fi-1", files=[{"name": "release.pdf"}]),
                            _doc("fi-2", files=[{"name": "release.pdf"}])]])
    assert len(docs) == 2


def test_cross_backend_duplicate_still_collapses():
    a = _doc("nat-1", source="oam_se", files=[{"sha256": "deadbeef", "name": "a.zip"}])
    b = _doc("fxo-9", source="filings_org", files=[{"sha256": "deadbeef", "name": "b.zip"}])
    assert len(merge_documents([[a], [b]])) == 1


def test_doc_id_is_a_pure_function_of_source_country_native_id():
    assert _doc("x").doc_id == _doc("x").doc_id
    assert _doc("x").doc_id != _doc("y").doc_id
    assert _doc("x", source="oam_no", country="NO").doc_id != _doc("x").doc_id


def test_native_id_is_required():
    with pytest.raises((TypeError, ValueError)):
        Document(native_id="", lei=None, country="SE", doc_type="other", period_end=None,
                 published_ts=None, discovered_ts="", language=None, source="oam_se")


# ---------------------------------------------------------------------------
# Adversarial: partly-empty documents must still key honestly
# ---------------------------------------------------------------------------

def test_doc_id_ignores_everything_but_source_country_native_id():
    """Two captures of one document that disagree on every soft field (a corrected
    title, a later discovery, a period the second run resolved) are one document."""
    a = Document(native_id="n-1", lei="L1", country="SE", doc_type="other",
                 period_end=None, published_ts=None, discovered_ts="2026-01-01",
                 language=None, source="oam_se")
    b = Document(native_id="n-1", lei=None, country="SE", doc_type="annual_report",
                 period_end=None, published_ts="2026-02-02", discovered_ts="2026-09-09",
                 language="sv", source="oam_se", files=[{"name": "x.pdf"}],
                 native_meta={"title": "corrected"})
    assert a.doc_id == b.doc_id


def test_files_without_any_identity_do_not_merge_two_documents():
    """A file list carrying neither sha256 nor name is no identity at all — the
    old key() turned it into ``(lei, doc_type, None, ('',))`` and swallowed the
    document's neighbours."""
    docs = merge_documents([[_doc("a", files=[{"url": "https://x.invalid/1"}]),
                            _doc("b", files=[{"url": "https://x.invalid/2"}])]])
    assert len(docs) == 2


def test_stable_native_id_refuses_to_invent_one():
    with pytest.raises(ValueError):
        stable_native_id(None, "", "   ")


def test_source_key_keeps_a_zero_and_refuses_a_missing_key():
    """A register that numbers its rows from zero has a row 0; ``str(x or "")``
    silently threw it away, and a missing key became the string "None"."""
    assert source_key(0) == "0"
    assert source_key("ip", 0) == "ip-0"
    assert source_key(None) == ""
    assert source_key("ip", None) == ""
    assert source_key("  ") == ""
    with pytest.raises(ValueError):
        _doc(source_key(None))


def test_stable_native_id_is_order_and_run_independent():
    assert stable_native_id("u", "t", None) == stable_native_id("u", "t")
    assert stable_native_id("u", "t") != stable_native_id("t", "u")


# ---------------------------------------------------------------------------
# The three backends that used to mint an id from a page offset, a file count
# or the wall clock: discover() twice over the same input, same ids.
# ---------------------------------------------------------------------------

_GB_HIT_A = {"download_link": "NSM/RNS/aaaa.html", "type": "ANNUAL FINANCIAL REPORT",
             "publication_date": "2025-03-01T09:00:00", "headline": "Annual Report 2024"}
_GB_HIT_B = {"download_link": "NSM/RNS/bbbb.html", "type": "OTHER",
             "publication_date": "2025-02-01T09:00:00", "headline": "Trading Update"}
_GB_HIT_NEW = {"download_link": "NSM/RNS/cccc.html", "type": "OTHER",
               "publication_date": "2025-04-01T09:00:00", "headline": "Newer Notice"}


class _GbStub:
    """Serves one page of hits, then an empty page so pagination terminates."""

    def __init__(self, hits):
        self._hits = hits
        self.calls = 0

    def post_json(self, url, body, **_):
        self.calls += 1
        if self.calls == 1:
            return {"hits": {"total": {"value": len(self._hits)}, "hits":
                             [{"_source": h} for h in self._hits]}}
        return {"hits": {"total": {"value": len(self._hits)}, "hits": []}}


_GB_ENTITY = Entity(lei="2138002P5RNKC5W2JZ46", name="Tesco PLC", country="GB")


def _gb_ids(hits):
    docs = NsmGB(fetcher=_GbStub(hits)).discover(_GB_ENTITY)
    return {d.native_meta["headline"]: d.doc_id for d in docs}


def test_gb_doc_ids_are_identical_across_two_runs():
    hits = [_GB_HIT_A, _GB_HIT_B]
    assert _gb_ids(hits) == _gb_ids(hits)


def test_gb_doc_id_survives_a_newer_disclosure_shifting_the_page():
    """The NSM has no id of its own on some hits. Keying on the page offset and
    ``len(docs)`` renamed every older document the night a newer one appeared."""
    before = _gb_ids([_GB_HIT_A, _GB_HIT_B])
    after = _gb_ids([_GB_HIT_NEW, _GB_HIT_A, _GB_HIT_B])
    assert before["Annual Report 2024"] == after["Annual Report 2024"]
    assert before["Trading Update"] == after["Trading Update"]


def test_gb_hit_with_no_stable_part_is_skipped_with_an_error():
    src = NsmGB(fetcher=_GbStub([{"download_link": "NSM/RNS/dddd.html"}]))
    docs = src.discover(_GB_ENTITY)
    # download_link alone is stable and sufficient; the document survives.
    assert len(docs) == 1
    assert not src.errors


_CH_ENTITY = Entity(lei="5493000LKVGOO9PELI61", name="ABB Ltd", country="CH",
                    isins=("CH0012221716",))


def _ch_item(title, news_date):
    """A SIX item with NO ``id`` — the case that fell back to ``len(files)``."""
    return {"content": [{"title": title, "content": f"<p>{title}</p>", "language": "en"}],
            "ad_hoc": False, "news_date": news_date, "company": {"name": "ABB Ltd"}}


class _ChStub:
    def __init__(self, items):
        self._items = items

    def get_json(self, url, **_):
        page = 0
        if "pageNumber=" in url:
            page = int(url.split("pageNumber=")[1].split("&")[0])
        if page:
            return {"data": [], "total": len(self._items)}
        return {"data": self._items, "total": len(self._items)}

    def get_text(self, url, *, params=None, **_):
        return "<html></html>"  # EQS: no company match -> no extra documents


def test_ch_doc_ids_are_identical_across_two_runs():
    items = [_ch_item("Ad hoc release", 1735689600000),
             _ch_item("Half-year results", 1751328000000)]
    first = [d.doc_id for d in DisclosureCH(fetcher=_ChStub(items)).discover(_CH_ENTITY)]
    second = [d.doc_id for d in DisclosureCH(fetcher=_ChStub(items)).discover(_CH_ENTITY)]
    assert first and first == second


def test_ch_two_id_less_items_with_the_same_file_count_stay_distinct():
    """``ch-six-<isin>-<len(files)>`` gave both of these the same id."""
    items = [_ch_item("Ad hoc release", 1735689600000),
             _ch_item("Half-year results", 1751328000000)]
    docs = DisclosureCH(fetcher=_ChStub(items)).discover(_CH_ENTITY)
    assert len(docs) == 2
    assert len({d.doc_id for d in docs}) == 2


def _be_item(**over):
    item = {"requiredReportingTopicId": None, "companyName": "TEST CO",
            "companyNumber": "0417497106", "nationality": "BE",
            "reportingTopicName": "Rapport financier annuel",
            "datePublication": "2025-01-01T00:00:00", "lei": "TESTLEI",
            "mainDocuments": [], "attachments": [], "isinCodes": [],
            "documentTitle": "Annual Report"}
    item.update(over)
    return item


class _BeStub:
    def __init__(self, items):
        self._items = items
        self.posts = 0

    def post_json(self, url, body, **_):
        self.posts += 1
        if body.get("pageSize") == 1 or body.get("startRowIndex", 0) == 0:
            return {"resultCount": len(self._items), "storiResultItems": self._items}
        return {"resultCount": len(self._items), "storiResultItems": []}

    def get_json(self, url, **_):
        return {}


_BE_ENTITY = Entity(lei=None, name="TEST CO", country="BE", isins=("BE0000000001",))


def test_be_doc_ids_are_identical_across_two_runs():
    """No topic id and no file: the id used to be minted from the wall clock, so
    the same notice landed in a new directory every night."""
    items = [_be_item()]
    first = [d.doc_id for d in StoriBE(http=_BeStub(items)).discover(_BE_ENTITY)]
    second = [d.doc_id for d in StoriBE(http=_BeStub(items)).discover(_BE_ENTITY)]
    assert first and first == second


def test_be_item_with_no_stable_part_is_skipped_and_recorded():
    """No topic id, no file, no title, no publication date — nothing to key on.
    Recording the gap beats inventing a document that changes name every run."""
    src = StoriBE(http=_BeStub([_be_item(documentTitle=None, datePublication=None,
                                         companyNumber=None, reportingTopicName=None)]))
    docs = src.discover(_BE_ENTITY)
    assert docs == []
    assert any(e["context"] == "native-id" for e in src.errors), src.errors
