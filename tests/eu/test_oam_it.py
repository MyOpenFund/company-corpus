"""Tests for the Italy OAM backend (OneInfoIT / 1Info JSON API).

All network-free: a stub fetcher routes get_json (companies) and post_json
(documenti / comunicati) from captured fixtures and synthetic data.
"""
from __future__ import annotations

import json
from pathlib import Path


from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_it import OneInfoIT, _normalise, _year_utc

FIX = Path(__file__).parent.parent / "fixtures" / "eu"

# -----------------------------------------------------------------------
# Stub fetcher
# -----------------------------------------------------------------------

class _StubFetcher:
    """Routes get_json (/companies/) and post_json (/API/Documenti|/API/Comunicati).

    The companies fixture (it_companies.json) does NOT contain ENI / ndg=117,
    so we inject it here to make the ndg-resolution path testable.
    """

    def __init__(self, documenti_fixture_text: str, companies_extra: list[dict] | None = None):
        raw_companies: list[dict] = json.loads((FIX / "it_companies.json").read_text())
        if companies_extra:
            raw_companies = raw_companies + companies_extra
        self._companies = raw_companies
        self._documenti = json.loads(documenti_fixture_text)

    def get_json(self, url: str, **_):
        if "/companies/" in url:
            return self._companies
        raise RuntimeError(f"Unexpected get_json url: {url}")

    def post_json(self, url: str, body, **_):
        start = body.get("start", 0)
        length = body.get("length", 500)
        if "/API/Documenti" in url:
            data = self._documenti.get("data") or []
            return self._page(data, start, length)
        if "/API/Comunicati" in url:
            return self._page([], start, length)
        raise RuntimeError(f"Unexpected post_json url: {url}")

    @staticmethod
    def _page(rows: list[dict], start: int, length: int) -> dict:
        """Emulate the 1Info DataTables paginator (start/length over a backing list)."""
        return {
            "draw": 1,
            "recordsTotal": len(rows),
            "recordsFiltered": len(rows),
            "data": rows[start:start + length],
        }


def _make_stub(companies_extra: list[dict] | None = None) -> _StubFetcher:
    return _StubFetcher(
        (FIX / "it_documenti.json").read_text(),
        companies_extra=companies_extra,
    )


# -----------------------------------------------------------------------
# Helper tests (pure logic)
# -----------------------------------------------------------------------

def test_normalise_collapses_whitespace_and_uppercases():
    assert _normalise("  COSTAMP  GROUP  S.P.A.  ") == "COSTAMP GROUP S.P.A."
    assert _normalise("ENI") == "ENI"
    assert _normalise("eni") == "ENI"


def test_year_utc_extracts_correct_year():
    # 1767207557 -> 2025 UTC
    assert _year_utc(1767207557) == "2025"
    # 1774292357 -> 2026 UTC
    assert _year_utc(1774292357) == "2026"
    assert _year_utc(None) is None


# -----------------------------------------------------------------------
# Core discover test
# -----------------------------------------------------------------------

def test_resolve_name_to_ndg_and_discover():
    """Discover for ENI resolves ndg=117 and returns Documents from the fixture."""
    stub = _make_stub(companies_extra=[{"ndg": 117, "descrizione": "ENI", "id": 0, "idRow": 0}])
    src = OneInfoIT(fetcher=stub)

    docs = src.discover(Entity(lei="L1", name="ENI", country="IT"))

    assert docs, "expected at least one Document"
    assert all(d.source == "oam-it" for d in docs)
    assert all(d.country == "IT" for d in docs)
    assert all(d.doc_type in DOC_TYPES for d in docs)
    assert all(d.files for d in docs), "every Document must have at least one file"
    assert all(
        f["url"].startswith("https://consob.1info.it")
        for d in docs
        for f in d.files
    )
    # Downloads live at the site ROOT; the /PORTALE1INFO/PdfViewer path 404s (verified
    # live). Regression guard so the API base never leaks into the download host again.
    assert all(
        f["url"].startswith("https://consob.1info.it/PdfViewer/PdfShow.aspx")
        and "/PORTALE1INFO/" not in f["url"]
        for d in docs
        for f in d.files
    )
    assert all(d.lei == "L1" for d in docs)
    assert all(d.language == "it" for d in docs)


def test_discover_paginates_beyond_one_page():
    """A single length-capped request truncates large issuers (ENI has ~660 documenti
    / ~1550 comunicati). discover() must page through and return EVERY row exactly once.
    """
    from company_corpus.eu.sources.oam_it import _PAGE

    n_rows = _PAGE * 2 + 37  # 1037 rows -> 3 pages, last partial
    rows = [
        {"pdf": f"DOC{i:05d}_oneinfo", "protocolCodeXbrl": None,
         "categoria": "2.2", "dataStoccaggio": 1767207557, "dataEsercizio": 1767207557}
        for i in range(n_rows)
    ]

    class _PagingStub:
        def __init__(self, doc_rows):
            self._rows = doc_rows
            self.documenti_calls = 0

        def get_json(self, url, **_):
            return [{"ndg": 117, "descrizione": "ENI", "id": 0, "idRow": 0}]

        def post_json(self, url, body, **_):
            start, length = body.get("start", 0), body.get("length", 500)
            rows = self._rows if "/API/Documenti" in url else []
            if "/API/Documenti" in url:
                self.documenti_calls += 1
            return {"draw": 1, "recordsTotal": len(rows),
                    "recordsFiltered": len(rows), "data": rows[start:start + length]}

    stub = _PagingStub(rows)
    src = OneInfoIT(fetcher=stub)
    docs = src.discover(Entity(lei="L1", name="ENI", country="IT"))

    assert len(docs) == n_rows, "every row must be returned exactly once (no truncation, no dupes)"
    ids = [d.files[0]["url"].split("file=")[1].split("&")[0] for d in docs]
    assert len(set(ids)) == n_rows, "no duplicate documents across pages"
    assert stub.documenti_calls == 3, "1037 rows / 500 per page = 3 requests"
    assert not src.errors


# -----------------------------------------------------------------------
# ESEF vs PDF URL construction
# -----------------------------------------------------------------------

def test_esef_file_uses_protocolcodexbrl_and_exercise_year():
    """Row with protocolCodeXbrl (.xbri, not a plain doc ext) → ESEF zip URL
    containing the protocolCodeXbrl value and the exercise year."""
    stub = _make_stub(companies_extra=[{"ndg": 117, "descrizione": "ENI", "id": 0, "idRow": 0}])
    src = OneInfoIT(fetcher=stub)
    docs = src.discover(Entity(lei="L1", name="ENI", country="IT"))

    # Find the document whose native_meta has a protocolCodeXbrl
    esef_docs = [d for d in docs if d.native_meta.get("protocolCodeXbrl")]
    assert esef_docs, "fixture row with protocolCodeXbrl=164624_oneinfo.xbri must yield an ESEF doc"

    esef_doc = esef_docs[0]
    esef_files = [f for f in esef_doc.files if f["kind"] == "esef"]
    assert esef_files, "ESEF Document must have an esef-kind file"

    esef_file = esef_files[0]
    xbrl_code = "164624_oneinfo.xbri"
    # URL must contain file=<protocolCodeXbrl>
    assert f"file={xbrl_code}" in esef_file["url"], (
        f"ESEF URL must use protocolCodeXbrl; got {esef_file['url']}"
    )
    # URL must use exercise year (2025) not storage year (2026)
    assert "year=2025" in esef_file["url"], (
        f"ESEF URL must use exercise year 2025; got {esef_file['url']}"
    )


def test_pdf_file_uses_pdf_id_and_storage_year():
    """Row with only pdf → document URL using pdf+'.pdf' and storage year."""
    # Use the first row of the fixture (no ESEF): storage=1779816940 -> year=2026
    stub = _make_stub(companies_extra=[{"ndg": 117, "descrizione": "ENI", "id": 0, "idRow": 0}])
    src = OneInfoIT(fetcher=stub)
    docs = src.discover(Entity(lei="L1", name="ENI", country="IT"))

    # Take a doc whose native_meta has no protocolCodeXbrl (pure PDF row)
    pdf_docs = [d for d in docs if not d.native_meta.get("protocolCodeXbrl")]
    assert pdf_docs, "expected at least one pure-PDF document from fixture"

    doc = pdf_docs[0]
    pdf_files = [f for f in doc.files if f["kind"] == "document"]
    assert pdf_files, "pure-PDF doc must have a document-kind file"

    f = pdf_files[0]
    pdf_id = doc.native_meta["pdf"]
    assert f["name"] == pdf_id + ".pdf"
    assert f"file={pdf_id}.pdf" in f["url"]
    storage_year = _year_utc(doc.native_meta["dataStoccaggio"])
    assert f"year={storage_year}" in f["url"]
    assert f["url"].startswith("https://consob.1info.it")


# -----------------------------------------------------------------------
# Synthetic row tests (not dependent on fixture content)
# -----------------------------------------------------------------------

def test_synthetic_esef_row_builds_correct_urls():
    """Synthetic row verifies URL construction logic end-to-end without fixture coupling."""

    class _SynthFetcher:
        def get_json(self, url, **_):
            return [{"ndg": 42, "descrizione": "SYNTH CO", "id": 0, "idRow": 0}]

        def post_json(self, url, body, **_):
            if "/API/Documenti" in url:
                return {
                    "draw": 1,
                    "recordsFiltered": 1,
                    "data": [{
                        "ndg": 42,
                        "dataStoccaggio": 1774292357,   # year 2026
                        "dataEsercizio": 1767207557,    # year 2025
                        "mittente": "SYNTH CO",
                        "oggetto": "Annual Report 2025 - ESEF",
                        "categoria": "1.1",
                        "protocolCode": "SYNTH001_oneinfo",
                        "protocolCodeXbrl": "SYNTH001_oneinfo.xbri",
                        "filetype": "documenti",
                        "pdf": "SYNTH001_oneinfo",
                        "idMercato": 6,
                    }],
                }
            return {"draw": 1, "recordsFiltered": 0, "data": []}

    src = OneInfoIT(fetcher=_SynthFetcher())
    docs = src.discover(Entity(lei="L42", name="SYNTH CO", country="IT"))

    assert len(docs) == 1
    doc = docs[0]

    esef_file = next(f for f in doc.files if f["kind"] == "esef")
    pdf_file = next(f for f in doc.files if f["kind"] == "document")

    assert "file=SYNTH001_oneinfo.xbri" in esef_file["url"]
    assert "year=2025" in esef_file["url"]   # exercise year

    assert "file=SYNTH001_oneinfo.pdf" in pdf_file["url"]
    assert "year=2026" in pdf_file["url"]    # storage year


def test_unknown_issuer_returns_empty():
    """Entity name not in the companies map → empty list."""
    stub = _make_stub()  # no extra; "UNKNOWN CORP" is absent
    src = OneInfoIT(fetcher=stub)
    docs = src.discover(Entity(lei="L_NONE", name="UNKNOWN CORP XYZ", country="IT"))
    assert docs == []


# -----------------------------------------------------------------------
# A-I3b regression: name-miss records resolve-no-match error
# -----------------------------------------------------------------------

def test_name_miss_records_resolve_no_match():
    """When the companies map loads successfully but entity name is absent,
    a 'resolve-no-match' error is recorded (peers: FI, GB, DK all do this).
    Old code returned None silently with no error recorded."""
    stub = _make_stub()  # companies map loaded; "UNKNOWN CORP XYZ" absent
    src = OneInfoIT(fetcher=stub)
    src.discover(Entity(lei="L_NONE", name="UNKNOWN CORP XYZ", country="IT"))

    resolve_errors = [e for e in src.errors if e["context"] == "resolve-no-match"]
    assert resolve_errors, (
        "expected a 'resolve-no-match' error for an entity whose name is absent "
        "from the 1Info company map"
    )


def test_failed_companies_load_not_cached():
    """A failed companies-endpoint fetch must not be cached as an empty map.
    Old code cached {} → every subsequent entity for the same src silently
    returned [] (no error).  Fix: don't cache on failure; next call retries."""
    call_count = [0]

    class _RetryableFetcher:
        def get_json(self, url, **_):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("transient")
            # Second and subsequent calls succeed
            return [{"ndg": 117, "descrizione": "ENI", "id": 0, "idRow": 0}]

        def post_json(self, url, body, **_):
            return {"draw": 1, "recordsFiltered": 0, "data": []}

    src = OneInfoIT(fetcher=_RetryableFetcher())

    # First call: load fails → records "companies" error, returns []
    docs1 = src.discover(Entity(lei="L1", name="ENI", country="IT"))
    assert docs1 == []
    assert any(e["context"] == "companies" for e in src.errors), (
        "first failed load must record a 'companies' error"
    )

    # Second call: same src, load retried (not cached from first failure) → succeeds
    docs2 = src.discover(Entity(lei="L1", name="ENI", country="IT"))
    assert docs2 == []  # POST returns empty data, but resolve succeeded
    # Only one "companies" error — the second load succeeded (no new error)
    assert sum(e["context"] == "companies" for e in src.errors) == 1, (
        "exactly one companies error (from the first failed load)"
    )
    # ENI found after retry → no resolve-no-match error
    assert not any(e["context"] == "resolve-no-match" for e in src.errors), (
        "ENI must resolve after successful retry"
    )
    assert call_count[0] == 2, (
        "companies endpoint must be called twice: once (failed) + once (retry)"
    )


def test_list_issuers_returns_empty():
    stub = _make_stub(companies_extra=[{"ndg": 117, "descrizione": "ENI", "id": 0, "idRow": 0}])
    src = OneInfoIT(fetcher=stub)
    assert src.list_issuers() == []


# -----------------------------------------------------------------------
# Error handling
# -----------------------------------------------------------------------

def test_discover_gracefully_handles_post_failure():
    """If both POST endpoints raise, discover returns [] and records errors."""

    class _FailPostFetcher:
        def get_json(self, url, **_):
            return [{"ndg": 1, "descrizione": "FAIL CO", "id": 0, "idRow": 0}]

        def post_json(self, url, body, **_):
            raise ConnectionError("network down")

    src = OneInfoIT(fetcher=_FailPostFetcher())
    docs = src.discover(Entity(lei="L1", name="FAIL CO", country="IT"))
    assert docs == []
    assert len(src.errors) == 2  # one per endpoint
    assert all(e["source"] == "oam-it" for e in src.errors)


def test_companies_fetch_error_returns_empty():
    """If the companies endpoint fails, discover returns [] and records the error."""

    class _FailGetFetcher:
        def get_json(self, url, **_):
            raise ConnectionError("network down")

        def post_json(self, url, body, **_):
            return {"draw": 1, "recordsFiltered": 0, "data": []}

    src = OneInfoIT(fetcher=_FailGetFetcher())
    docs = src.discover(Entity(lei="L1", name="ENI", country="IT"))
    assert docs == []
    assert any(e["context"] == "companies" for e in src.errors)


# -----------------------------------------------------------------------
# Category mapping
# -----------------------------------------------------------------------

def test_categoria_mapping():
    """All fixture rows have categoria=1.1 -> annual_report."""
    stub = _make_stub(companies_extra=[{"ndg": 117, "descrizione": "ENI", "id": 0, "idRow": 0}])
    src = OneInfoIT(fetcher=stub)
    docs = src.discover(Entity(lei="L1", name="ENI", country="IT"))
    assert docs
    assert all(d.doc_type == "annual_report" for d in docs)


def test_unknown_categoria_maps_to_other():
    """Unknown categoria code → 'other' doc_type."""

    class _SynthFetcher:
        def get_json(self, url, **_):
            return [{"ndg": 1, "descrizione": "CO", "id": 0, "idRow": 0}]

        def post_json(self, url, body, **_):
            if "/API/Documenti" in url:
                return {
                    "draw": 1,
                    "recordsFiltered": 1,
                    "data": [{
                        "ndg": 1, "dataStoccaggio": 1767207557, "dataEsercizio": 1767207557,
                        "mittente": "CO", "oggetto": "Test", "categoria": "9.9",
                        "protocolCode": "X", "protocolCodeXbrl": None,
                        "filetype": "documenti", "pdf": "X_oneinfo", "idMercato": 1,
                    }],
                }
            return {"draw": 1, "recordsFiltered": 0, "data": []}

    src = OneInfoIT(fetcher=_SynthFetcher())
    docs = src.discover(Entity(lei="L1", name="CO", country="IT"))
    assert docs
    assert docs[0].doc_type == "other"
