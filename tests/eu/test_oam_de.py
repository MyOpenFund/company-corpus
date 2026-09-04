"""Tests for the Germany OAM backend (BundesanzeigerDE — stateful Wicket scrape).

All network-free: a stub fetcher routes get_text (landing + detail) and post_text
(search) from the captured real fixtures and synthetic HTML.
"""
from __future__ import annotations

from pathlib import Path

from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_de import BundesanzeigerDE, _doc_type, _parse_date

FIX = Path(__file__).parent.parent / "fixtures" / "eu"
SEARCH_HTML = (FIX / "de_kapitalmarkt_search_sap.html").read_text()
DETAIL_HTML = (FIX / "de_detail_publication.html").read_text()


# -----------------------------------------------------------------------
# Stub fetcher
# -----------------------------------------------------------------------

class _StubFetcher:
    """Routes the three call kinds by URL substring.

    * ``get_text`` on a ``suche-`` landing → the search-results fixture (which also
      carries a search form with both fulltext + search-button, so it doubles as the
      landing for action discovery).
    * ``get_text`` on a ``~publication~link`` detail link → the detail fixture.
    * ``post_text`` (the search) → the search-results fixture.

    A second register slug (``suche-rechnungslegung``) returns an empty results page
    so only the Kapitalmarkt register contributes Documents in the fixture-driven
    tests (keeps doc counts deterministic).
    """

    # The empty register's form action carries a marker (EMPTY) so post_text can
    # route its search to a results-free page — otherwise the shared post_text would
    # return the SAP rows for BOTH registers and double every Document.
    EMPTY = '<html><form action="https://www.bundesanzeiger.de/pub/de/suchen2?EMPTY">' \
            '<input name="fulltext"/><input name="search-button"/></form>' \
            '<div class="row sticky-top result_header"></div></html>'

    def __init__(self, search_html=SEARCH_HTML, detail_html=DETAIL_HTML):
        self._search = search_html
        self._detail = detail_html
        self.detail_calls = 0

    def get_text(self, url, **_):
        if "publication~link" in url:
            self.detail_calls += 1
            return self._detail
        if "pagination~link" in url:
            return self.EMPTY  # a results-free page → pagination terminates cleanly
        if "suche-rechnungslegung" in url:
            return self.EMPTY
        if "suche-" in url:
            return self._search  # landing carries the search form
        raise RuntimeError(f"Unexpected get_text url: {url}")

    def post_text(self, url, data, **_):
        if "EMPTY" in url:
            return self.EMPTY
        return self._search


# -----------------------------------------------------------------------
# Core fixture-driven tests
# -----------------------------------------------------------------------

def test_parses_and_filters_to_target_issuer():
    src = BundesanzeigerDE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="SAP SE", country="DE"))

    assert docs, "expected at least one Document"
    assert all(d.source == "oam-de" and d.country == "DE" for d in docs)
    assert all(d.language == "de" and d.lei == "L1" for d in docs)
    assert all(d.doc_type in DOC_TYPES for d in docs)
    # Every kept Document was published BY SAP (the Bridgewater noise rows dropped).
    assert all(
        d.native_meta["publishing_entity"].casefold().startswith("sap se")
        for d in docs
    )
    assert all(len(d.files) == 1 and d.files[0]["kind"] == "html" for d in docs)
    # The real fixture contains noise rows (e.g. Bridgewater) that the filter drops;
    # assert that drop is RECORDED so the filter path is exercised and never silent.
    assert any(e["context"] == "issuer-filter" for e in src.errors), \
        "dropped noise rows must be recorded (never silently partial)"


def test_inline_content_captured():
    src = BundesanzeigerDE(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="SAP SE", country="DE"))

    assert docs
    for d in docs:
        f = d.files[0]
        assert f.get("content"), "kept Documents must carry inline detail HTML"
        assert "Dividende" in f["content"] or "SAP" in f["content"]
        assert f["url"].startswith("https://www.bundesanzeiger.de"), "provenance url"
        assert f["name"] == f"{d.doc_id}.html"


def test_doc_type_mapping():
    assert _doc_type("Halbjahresfinanzbericht 2025") == "half_year_report"
    assert _doc_type("Jahresabschluss zum 31.12.2024") == "annual_report"
    assert _doc_type("Jahres- und Konzernabschluss") == "annual_report"
    assert _doc_type("Gesamtzahl der Stimmrechte gem. § 41 WpHG") == "holding_notification"
    assert _doc_type("Einladung zur ordentlichen Hauptversammlung") == "governance"
    assert _doc_type("Dividendenbekanntmachung") == "other"
    assert _doc_type("Ad hoc: Insiderinformation nach Art. 17 MAR") == "inside_information"


def test_parse_date():
    assert _parse_date("06.05.2026") == "2026-05-06"
    assert _parse_date("nonsense") is None
    assert _parse_date("31.13.2024") is None  # invalid month


# -----------------------------------------------------------------------
# Issuer filter (synthetic)
# -----------------------------------------------------------------------

_SYNTH_RESULTS = """
<html>
<form action="https://www.bundesanzeiger.de/pub/de/suchen2?2-1.-search~form~panel-search~form">
  <input name="fulltext"/><input name="search-button"/>
</form>
<div class="row sticky-top result_header"><div class="first"></div></div>
<div class="row">
  <div class="first">SAP SE<br/>Walldorf</div>
  <div class="part">Kapitalmarkt</div>
  <div class="info"><a href="https://www.bundesanzeiger.de/pub/de/suchen2?2-1.-search~table~panel-rows-0-search~table~row~panel-publication~link">Dividendenbekanntmachung</a></div>
  <div class="date">06.05.2026</div>
</div>
<div class="row back">
  <div class="first">Bridgewater Associates, LP<br/>Connecticut</div>
  <div class="part">Kapitalmarkt</div>
  <div class="info"><a href="https://www.bundesanzeiger.de/pub/de/suchen2?2-1.-search~table~panel-rows-1-search~table~row~panel-publication~link">Mitteilung von Netto-Leerverkaufspositionen</a></div>
  <div class="date">01.06.2026</div>
</div>
</html>
"""


class _SynthFetcher:
    def __init__(self, results):
        self._results = results

    def get_text(self, url, **_):
        if "publication~link" in url:
            return "<html>detail body</html>"
        if "suche-rechnungslegung" in url:
            return _StubFetcher.EMPTY
        return self._results

    def post_text(self, url, data, **_):
        if "EMPTY" in url:
            return _StubFetcher.EMPTY
        return self._results


def test_issuer_filter_drops_other_entities():
    src = BundesanzeigerDE(fetcher=_SynthFetcher(_SYNTH_RESULTS))
    docs = src.discover(Entity(lei="L1", name="SAP SE", country="DE"))

    assert len(docs) == 1, "only the SAP row survives; Bridgewater is dropped"
    assert docs[0].native_meta["publishing_entity"].startswith("SAP SE")
    # The drop must be recorded, never silent.
    assert any(e["context"] == "issuer-filter" for e in src.errors)


def test_unparseable_or_empty_name_returns_empty():
    src = BundesanzeigerDE(fetcher=_SynthFetcher(_SYNTH_RESULTS))
    assert src.discover(Entity(lei="L1", name="", country="DE")) == []


# -----------------------------------------------------------------------
# Robustness
# -----------------------------------------------------------------------

def test_detail_fetch_failure_emits_document_without_content():
    """A failing detail GET must still yield the Document (index survives) MINUS
    content, with the failure recorded — never silently dropped."""

    class _DetailFailFetcher(_SynthFetcher):
        def get_text(self, url, **_):
            if "publication~link" in url:
                raise ConnectionError("detail down")
            if "suche-rechnungslegung" in url:
                return _StubFetcher.EMPTY
            return self._results

    src = BundesanzeigerDE(fetcher=_DetailFailFetcher(_SYNTH_RESULTS))
    docs = src.discover(Entity(lei="L1", name="SAP SE", country="DE"))

    assert len(docs) == 1
    f = docs[0].files[0]
    assert "content" not in f, "no content when capture failed"
    # The session-bound url is dropped so download.py won't re-fetch a stale page;
    # it is marked index-only and the link survives as provenance in native_meta.
    assert "url" not in f and f.get("capture_failed") is True
    assert docs[0].native_meta["detail_url"]  # provenance preserved
    assert any(e["context"] == "detail" for e in src.errors)


def test_doc_id_is_deterministic():
    src = BundesanzeigerDE(fetcher=_SynthFetcher(_SYNTH_RESULTS))
    e = Entity(lei="L1", name="SAP SE", country="DE")
    ids1 = [d.doc_id for d in src.discover(e)]
    ids2 = [d.doc_id for d in BundesanzeigerDE(fetcher=_SynthFetcher(_SYNTH_RESULTS)).discover(e)]
    assert ids1 == ids2 and all(i.startswith("de-") for i in ids1)


def test_list_issuers_returns_empty():
    assert BundesanzeigerDE(fetcher=_StubFetcher()).list_issuers() == []
