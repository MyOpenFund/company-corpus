"""Tests for the Netherlands OAM backend (AfmNL — AFM bulk XML export).

All network-free: a stub fetcher routes get_text from real captured fixtures
(nl_export_verslaggeving.xml, nl_details_asml.html) and a minimal register-page stub.

RED -> GREEN discipline: tests were written before the implementation.
"""
from __future__ import annotations

from pathlib import Path

from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_nl import AfmNL, _normalise

FIX = Path(__file__).parent.parent / "fixtures" / "eu"

_EXPORT_XML = (FIX / "nl_export_verslaggeving.xml").read_text()
_DETAILS_HTML = (FIX / "nl_details_asml.html").read_text()

# A minimal register-page stub (politeness/cookie bootstrap only).
_REGISTER_PAGE_HTML = "<html><body>AFM register page stub</body></html>"

# ---------------------------------------------------------------------------
# Stub fetcher
# ---------------------------------------------------------------------------

class _StubFetcher:
    """Routes get_text calls by URL substring.

    * register page URL -> minimal stub HTML (cookie bootstrap).
    * export.aspx URL   -> the real trimmed XML export fixture.
    * details?id=       -> the real ASML details fixture (enc link present).
    """

    def __init__(
        self,
        *,
        register_html: str = _REGISTER_PAGE_HTML,
        export_xml: str = _EXPORT_XML,
        details_html: str = _DETAILS_HTML,
    ):
        self._register = register_html
        self._export = export_xml
        self._details = details_html
        self.get_calls: list[str] = []

    def get_text(self, url: str, **_) -> str:
        self.get_calls.append(url)
        if "export.aspx" in url:
            return self._export
        if "details?id=" in url:
            return self._details
        if "financiele-verslaggeving" in url:
            return self._register
        raise RuntimeError(f"Unexpected get_text url: {url}")


# ---------------------------------------------------------------------------
# Helper / pure-logic tests
# ---------------------------------------------------------------------------

def test_normalise_strips_suffix_and_diacritics():
    """_normalise strips trailing N.V./B.V. and diacritics."""
    assert _normalise("ASML Holding N.V.") == "asml holding"
    assert _normalise("Heineken N.V.") == "heineken"
    assert _normalise("Some Corp B.V.") == "some corp"
    # Diacritic stripping: é -> e
    assert _normalise("Société Générale N.V.") == "societe generale"
    # Whitespace collapse
    assert _normalise("  ASML  Holding  N.V.  ") == "asml holding"


# ---------------------------------------------------------------------------
# Core fixture-driven tests
# ---------------------------------------------------------------------------

def test_parses_export_and_filters_to_issuer():
    """discover() for ASML returns ≥1 Document; the annual ASML entry is present
    with correct doc_type, esef kind, and a downloadregisterfile enc= URL."""
    src = AfmNL(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="ASML Holding N.V.", country="NL"))

    assert docs, "expected at least one Document for ASML"
    assert all(d.doc_type in DOC_TYPES for d in docs)

    annual = [d for d in docs if d.doc_type == "annual_report"]
    assert annual, "expected an annual_report Document for ASML"

    doc = annual[0]
    assert doc.lei == "L1"
    assert doc.country == "NL"
    assert doc.language == "nl"
    assert doc.source == "oam-nl"

    assert doc.files, "Document must have at least one file"
    f = doc.files[0]
    assert f["url"].startswith("https://www.afm.nl/downloadregisterfile.aspx"), \
        f"expected downloadregisterfile URL, got {f['url']!r}"
    assert "enc=" in f["url"], "URL must contain enc= token"
    assert f["kind"] == "esef", f"filename .xbri must yield kind=esef, got {f['kind']!r}"


def test_doc_type_mapping():
    """Annual financial report -> annual_report; Half-yearly -> half_year_report.

    The export fixture contains ASML (annual .xbri) and Pepco (half-yearly .pdf).
    We test ASML directly; for Pepco we use a stub that returns the details fixture.
    """
    src = AfmNL(fetcher=_StubFetcher())

    asml_docs = src.discover(Entity(lei="L1", name="ASML Holding N.V.", country="NL"))
    assert any(d.doc_type == "annual_report" for d in asml_docs), \
        "ASML row (Annual financial report) must map to annual_report"

    pepco_docs = AfmNL(fetcher=_StubFetcher()).discover(
        Entity(lei="L2", name="Pepco Group N.V.", country="NL")
    )
    assert any(d.doc_type == "half_year_report" for d in pepco_docs), \
        "Pepco row (Half-yearly financial report) must map to half_year_report"


def test_issuer_filter_exact_no_guess():
    """Prefix-only match and a totally absent name both return [] (strict exact match)."""
    src = AfmNL(fetcher=_StubFetcher())

    # "ASML" normalises to "asml" which does NOT equal "asml holding" -> no match
    docs_prefix = src.discover(Entity(lei="L1", name="ASML", country="NL"))
    assert docs_prefix == [], \
        "prefix-only match must not bind any document (strict no-guess)"

    src2 = AfmNL(fetcher=_StubFetcher())
    docs_absent = src2.discover(Entity(lei="L2", name="Nonexistent Corp N.V.", country="NL"))
    assert docs_absent == [], "absent issuer must return []"


def test_details_hop_failure_is_index_only():
    """If the details GET raises, the Document is still emitted with capture_failed=True
    and an error is recorded — the rest of the pipeline must not crash."""

    class _FailDetailsFetcher:
        def get_text(self, url: str, **_) -> str:
            if "export.aspx" in url:
                return _EXPORT_XML
            if "financiele-verslaggeving" in url:
                return _REGISTER_PAGE_HTML
            if "details?id=" in url:
                raise ConnectionError("details endpoint down")
            raise RuntimeError(f"Unexpected url: {url}")

    src = AfmNL(fetcher=_FailDetailsFetcher())
    docs = src.discover(Entity(lei="L1", name="ASML Holding N.V.", country="NL"))

    assert docs, "Document must still be emitted when details hop fails"
    f = docs[0].files[0]
    assert f.get("capture_failed") is True, "capture_failed must be set"
    assert "url" not in f, "no url when capture failed"
    assert any(e["context"] == "details" for e in src.errors), \
        "details error must be recorded"


def test_published_ts_parsed():
    """AFM datum format M/D/YYYY h:mm:ss AM -> ISO date string YYYY-MM-DD."""
    src = AfmNL(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="ASML Holding N.V.", country="NL"))

    assert docs
    doc = docs[0]
    # ASML fixture datum: "2/25/2026 11:11:34 AM" -> "2026-02-25"
    assert doc.published_ts == "2026-02-25", \
        f"expected '2026-02-25', got {doc.published_ts!r}"


def test_empty_name_returns_empty():
    """Entity with no name must return [] without hitting the network."""
    fetcher = _StubFetcher()
    src = AfmNL(fetcher=fetcher)
    docs = src.discover(Entity(lei="L1", name="", country="NL"))
    assert docs == []
    assert fetcher.get_calls == [], "no GET calls expected for empty name"


def test_export_fetch_failure_returns_empty_and_records_error():
    """If the export GET fails, discover returns [] and records the error."""

    class _FailExportFetcher:
        def get_text(self, url: str, **_) -> str:
            if "financiele-verslaggeving" in url and "export" not in url:
                return _REGISTER_PAGE_HTML
            raise ConnectionError("export endpoint down")

    src = AfmNL(fetcher=_FailExportFetcher())
    docs = src.discover(Entity(lei="L1", name="ASML Holding N.V.", country="NL"))
    assert docs == []
    assert any(e["context"] == "export" for e in src.errors), \
        "export error must be recorded"


def test_list_issuers_returns_empty():
    assert AfmNL(fetcher=_StubFetcher()).list_issuers() == []


def test_native_meta_carries_boekjaar_and_register():
    """native_meta must carry boekjaar, filename, and register."""
    src = AfmNL(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="ASML Holding N.V.", country="NL"))

    assert docs
    meta = docs[0].native_meta
    assert meta.get("boekjaar") == "2025", f"expected boekjaar=2025, got {meta.get('boekjaar')!r}"
    assert meta.get("register") == "financiele-verslaggeving"
    assert "filename" in meta


def test_doc_id_prefixed_nl():
    """doc_id must start with 'nl-'."""
    src = AfmNL(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="ASML Holding N.V.", country="NL"))
    assert all(d.doc_id.startswith("nl-") for d in docs)


# ---------------------------------------------------------------------------
# acquire.py wiring
# ---------------------------------------------------------------------------

def test_country_backends_includes_nl():
    from company_corpus.eu.acquire import COUNTRY_BACKENDS
    assert "NL" in COUNTRY_BACKENDS
    assert COUNTRY_BACKENDS["NL"] is AfmNL
