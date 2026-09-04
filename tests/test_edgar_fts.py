from __future__ import annotations

from company_corpus.sources.edgar_fts import OFFERING_FORMS, EdgarFTS
from tests.conftest import FakeFetcher

EFTS_HIT = {"hits": {"hits": [
    {"_source": {"ciks": ["0000320193"], "display_names": ["APPLE INC (AAPL)"]}}]}}
EFTS_EMPTY = {"hits": {"hits": []}}


def test_resolve_returns_cik_and_name_from_top_offering_hit(config):
    fetcher = FakeFetcher({"037833AT7": EFTS_HIT}, config=config)
    fts = EdgarFTS(fetcher=fetcher)
    assert fts.resolve("037833AT7") == ("0000320193", "APPLE INC (AAPL)")
    # query was restricted to offering forms
    assert "forms=" in fetcher.calls[0]
    assert "424B2" in fetcher.calls[0] and "FWP" in fetcher.calls[0]


def test_offering_forms_has_no_slash_amendment_tokens():
    # EFTS returns zero hits if the forms filter contains a slash token like
    # "S-3/A" (it rejects amendment codes), so only base codes are allowed.
    assert "/" not in OFFERING_FORMS


def test_resolve_no_hit_returns_none(config):
    fts = EdgarFTS(fetcher=FakeFetcher({"999999999": EFTS_EMPTY}, config=config))
    assert fts.resolve("999999999") is None


def test_resolve_fetch_error_returns_none_and_records(config):
    # No route for this CUSIP -> FakeFetcher raises -> resolve swallows + records.
    fts = EdgarFTS(fetcher=FakeFetcher({}, config=config))
    assert fts.resolve("12345ABC9") is None
    assert len(fts.errors) == 1 and fts.errors[0]["source"] == "edgar_fts"


def test_offering_forms_excludes_holdings_forms():
    # The restriction is the whole point: no 13F/N-PORT in the form filter.
    assert "13F" not in OFFERING_FORMS and "NPORT" not in OFFERING_FORMS.replace("-", "")
