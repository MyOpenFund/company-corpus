"""Tests for the Spain OAM backend (CnmvES — CNMV WebForms scrape).

All network-free: a stub fetcher routes get_text and post_text from the
captured real fixtures (es_busqueda_entidad_iberdrola.html,
es_resultado_ip_iberdrola.html) and synthetic HTML for the other registers
(annual financial reports, other relevant info).

RED → GREEN discipline: each test was first written before the implementation
existed, then the implementation was added.
"""
from __future__ import annotations

from pathlib import Path


from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_es import CnmvES, _normalise, _parse_date

FIX = Path(__file__).parent.parent / "fixtures" / "eu"

# Real captured fixture HTML
_BUSQUEDA_HTML = (FIX / "es_busqueda_entidad_iberdrola.html").read_text()
_RESULTADO_IP_HTML = (FIX / "es_resultado_ip_iberdrola.html").read_text()
_LISTADO_IFA_HTML = (FIX / "es_listadoifa_iberdrola.html").read_text()

# A direct-hit page: a uniquely-matching name skips the <select> and the site serves
# the entity's DatosEntidad page straight away (issuer form: nif first in the query).
_DIRECT_HIT_HTML = """
<html><head>
<meta name="title" content="CNMV - Información de la Entidad - ACME POWER, S.A." />
<link rel="canonical" href="https://www.cnmv.es/portal/consultas/datosentidad?nif=A-99999999&lang=es" />
</head><body>ACME POWER, S.A.</body></html>
"""
# A credit-institution direct hit: nif sits AFTER numero/tipo in the query string.
_DIRECT_HIT_BANK_HTML = """
<html><head>
<meta name="title" content="CNMV - Información de la Entidad - BANCO ACME, S.A." />
<link rel="canonical" href="https://www.cnmv.es/portal/consultas/datosentidad?numero=49&tipo=ECN&nif=A12345678&lang=es" />
</head><body>BANCO ACME, S.A.</body></html>
"""

# Minimal landing HTML with the three WebForms hidden fields.
_LANDING_HTML = """
<html>
<form method="post" action="/portal/Consultas/BusquedaPorEntidad">
  <input type="hidden" name="__VIEWSTATE" value="VIEWSTATE_DUMMY" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="VSGEN_DUMMY" />
  <input type="hidden" name="__EVENTVALIDATION" value="EVVAL_DUMMY" />
  <input type="text" name="ctl00$ContentPrincipal$txtBusqueda" />
  <input type="submit" name="ctl00$ContentPrincipal$btnBuscar" value="Buscar" />
</form>
</html>
"""

# Minimal empty register page (no verdocumento rows).
_EMPTY_REGISTER_HTML = """
<html><body><ul id="listaElementosPrimernivel"></ul></body></html>
"""


# ---------------------------------------------------------------------------
# Stub fetcher
# ---------------------------------------------------------------------------

class _StubFetcher:
    """Routes calls by URL substring.

    * get_text on BusquedaPorEntidad → the synthetic landing (3 hidden fields).
    * post_text on BusquedaPorEntidad → the captured Iberdrola options fixture.
    * get_text on resultado-ip → the captured resultado-ip fixture.
    * get_text on em_inffinanual or resultado-oir → empty register page.
    """

    def __init__(
        self,
        *,
        landing_html: str = _LANDING_HTML,
        post_html: str = _BUSQUEDA_HTML,
        resultado_ip_html: str = _RESULTADO_IP_HTML,
        ifa_html: str = _LISTADO_IFA_HTML,
        empty_html: str = _EMPTY_REGISTER_HTML,
    ):
        self._landing = landing_html
        self._post = post_html
        self._ip = resultado_ip_html
        self._ifa = ifa_html
        self._empty = empty_html
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []

    def get_text(self, url: str, **_) -> str:
        self.get_calls.append(url)
        if 'BusquedaPorEntidad' in url:
            return self._landing
        if 'ListadoIFA' in url:
            return self._ifa
        if 'resultado-ip' in url:
            return self._ip
        if 'resultado-oir' in url:
            return self._empty
        raise RuntimeError(f"Unexpected get_text url: {url}")

    def post_text(self, url: str, data, **_) -> str:
        self.post_calls.append((url, dict(data)))
        if 'BusquedaPorEntidad' in url:
            return self._post
        raise RuntimeError(f"Unexpected post_text url: {url}")


# ---------------------------------------------------------------------------
# Helper / pure-logic tests
# ---------------------------------------------------------------------------

def test_normalise_collapses_whitespace_and_strips_suffix():
    assert _normalise("IBERDROLA, S.A.") == "iberdrola"
    assert _normalise("iberdrola, s.a.") == "iberdrola"
    assert _normalise("  ENI   S.P.A.  ") == "eni"


def test_normalise_strips_diacritics():
    # CNMV stores accent-folded names (TELEFONICA); GLEIF keeps them (Telefónica).
    assert _normalise("Telefónica, S.A.") == "telefonica"
    assert _normalise("Telefónica, S.A.") == _normalise("TELEFONICA, S.A.")
    assert _normalise("IBERDROLA  FINANCIACIÓN  S.A.") == "iberdrola financiacion"


def test_parse_date_dd_mm_yyyy():
    assert _parse_date("24/09/2025") == "2025-09-24"
    assert _parse_date("01/01/2024") == "2024-01-01"
    assert _parse_date("garbage") is None
    assert _parse_date("") is None


# ---------------------------------------------------------------------------
# NIF resolution tests
# ---------------------------------------------------------------------------

def test_resolve_name_to_nif_exact_match():
    """'IBERDROLA, S.A.' (and lowercase) must resolve to A-48010615, not a subsidiary."""
    src = CnmvES(fetcher=_StubFetcher())
    nif = src._resolve_nif("IBERDROLA, S.A.")
    assert nif == "A-48010615", f"expected A-48010615, got {nif!r}"
    assert not src.errors, f"no errors expected; got {src.errors}"


def test_resolve_name_to_nif_case_insensitive():
    """Lowercase name resolves identically."""
    src = CnmvES(fetcher=_StubFetcher())
    nif = src._resolve_nif("iberdrola, s.a.")
    assert nif == "A-48010615"


def test_resolve_ambiguous_or_missing_returns_none():
    """A name that matches no option exactly (e.g. just 'IBERDROLA') → None + error.

    'IBERDROLA' after normalisation is 'iberdrola'; the fixture options are
    'IBERDROLA, S.A.' → 'iberdrola' (stripped suffix), 'IBERDROLA FINANCIACIÓN S.A.'
    → 'iberdrola financiación', etc. So 'iberdrola' would match 'IBERDROLA, S.A.' only.

    Use a name that genuinely has no option: 'IBERDROLA GROUP NONEXISTENT'.
    """
    src = CnmvES(fetcher=_StubFetcher())
    nif = src._resolve_nif("IBERDROLA GROUP NONEXISTENT")
    assert nif is None
    assert any(
        e["context"] in ("resolve-no-match", "resolve-ambiguous")
        for e in src.errors
    ), f"expected a resolution error; got {src.errors}"


def test_resolve_direct_hit_issuer_page():
    """A uniquely-matching name skips the <select> and redirects to DatosEntidad;
    the NIF is recovered from that page when its entity name still matches exactly."""
    src = CnmvES(fetcher=_StubFetcher(post_html=_DIRECT_HIT_HTML))
    assert src._resolve_nif("ACME POWER, S.A.") == "A-99999999"
    assert not src.errors


def test_resolve_direct_hit_credit_institution_nif_anywhere_in_query():
    """Credit institutions put nif= after numero/tipo in the DatosEntidad URL."""
    src = CnmvES(fetcher=_StubFetcher(post_html=_DIRECT_HIT_BANK_HTML))
    assert src._resolve_nif("Banco Acme, S.A.") == "A12345678"


def test_resolve_direct_hit_rejected_when_name_mismatches():
    """A direct-hit page whose entity name does NOT match the target is rejected
    (strict no-guess) — never bind an unrelated NIF."""
    src = CnmvES(fetcher=_StubFetcher(post_html=_DIRECT_HIT_HTML))
    assert src._resolve_nif("TOTALLY DIFFERENT CO, S.A.") is None
    assert any(e["context"] == "resolve-no-match" for e in src.errors)


def test_discover_ifa_annual_reports():
    """The IFA table yields one annual_report Document per fiscal year, each carrying
    its report files (individual/consolidated + ESEF zip), with a real period_end."""
    src = CnmvES(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="IBERDROLA, S.A.", country="ES"))
    annual = [d for d in docs if d.doc_type == "annual_report"]
    assert annual, "expected annual_report Documents from the IFA table"
    assert all(d.period_end is not None for d in annual), "period_end from Fecha Estados Financieros"
    assert all(d.files for d in annual)
    # Every annual file is an ABSOLUTE CNMV document URL (verdocumento or AUDITA);
    # the relative /AUDITA/… hrefs must have been joined to the host.
    assert all(
        f["url"].startswith("https://www.cnmv.es/")
        and ("verdocumento/ver" in f["url"] or "/AUDITA/" in f["url"])
        for d in annual for f in d.files
    )
    # At least one row exposes the ESEF (ZIP/Xbri) package.
    assert any(f["kind"] == "esef" for d in annual for f in d.files)


def test_resolve_caches_nif():
    """A second call with the same (normalised) name must not hit the network again."""
    fetcher = _StubFetcher()
    src = CnmvES(fetcher=fetcher)
    src._resolve_nif("IBERDROLA, S.A.")
    calls_after_first = len(fetcher.get_calls)
    src._resolve_nif("IBERDROLA, S.A.")
    assert len(fetcher.get_calls) == calls_after_first, "cache must prevent a second GET"


# ---------------------------------------------------------------------------
# discover() integration tests
# ---------------------------------------------------------------------------

def test_discover_parses_verdocumento_rows():
    """discover() for IBERDROLA, S.A. must yield Documents from the resultado-ip fixture.

    Each Document must:
    - have doc_type in DOC_TYPES
    - have exactly one file with kind=='document' and a verdocumento URL
    - have no inline 'content' key (standard download path)
    - carry the entity's LEI
    - have language=='es' and source=='oam-es'
    """
    src = CnmvES(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="IBERDROLA, S.A.", country="ES"))

    assert docs, "expected at least one Document from the resultado-ip fixture"
    assert all(d.doc_type in DOC_TYPES for d in docs)
    assert all(d.source == "oam-es" for d in docs)
    assert all(d.country == "ES" for d in docs)
    assert all(d.language == "es" for d in docs)
    assert all(d.lei == "L1" for d in docs)

    # The <li>-style registers (inside_information / other) yield single-file docs
    # with a ?t=GUID verdocumento URL (annual_report IFA rows are tested separately).
    li_docs = [d for d in docs if d.doc_type != "annual_report"]
    assert li_docs
    for doc in li_docs:
        assert len(doc.files) == 1
        f = doc.files[0]
        assert f["kind"] == "document", f"expected kind=document, got {f['kind']!r}"
        assert f["url"].startswith(
            "https://www.cnmv.es/webservices/verdocumento/ver?t="
        ), f"unexpected URL: {f['url']!r}"
        assert "content" not in f, "verdocumento files must NOT carry inline content"


def test_discover_resultado_ip_has_correct_doc_type():
    """Documents parsed from resultado-ip must have doc_type='inside_information'."""
    src = CnmvES(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L1", name="IBERDROLA, S.A.", country="ES"))
    ip_docs = [d for d in docs if d.doc_type == "inside_information"]
    assert ip_docs, "at least one inside_information doc expected from resultado-ip"


def test_doc_type_per_register():
    """The <li>-register mapping is ip→inside_information, oir→other; annual reports
    come from the separate IFA table register, not _REGISTERS."""
    from company_corpus.eu.sources.oam_es import _REGISTERS, _IFA_URL

    mapping = {frag: dt for frag, dt in _REGISTERS}
    assert "inside_information" in mapping.values()
    assert "other" in mapping.values()
    assert "annual_report" not in mapping.values(), "annual reports are the IFA register"
    assert any("resultado-ip" in k for k in mapping.keys())
    assert any("resultado-oir" in k for k in mapping.keys())
    assert "ListadoIFA" in _IFA_URL


def test_empty_name_returns_empty():
    """Entity with empty name must return [] without hitting the network."""
    fetcher = _StubFetcher()
    src = CnmvES(fetcher=fetcher)
    docs = src.discover(Entity(lei="L1", name="", country="ES"))
    assert docs == []
    # No network calls should have been made.
    assert fetcher.get_calls == [], "no GET expected for empty name"
    assert fetcher.post_calls == [], "no POST expected for empty name"


def test_list_issuers_returns_empty():
    src = CnmvES(fetcher=_StubFetcher())
    assert src.list_issuers() == []


def test_doc_id_is_deterministic():
    """doc_id must be stable across two independent discover() calls."""
    e = Entity(lei="L1", name="IBERDROLA, S.A.", country="ES")
    ids1 = [d.doc_id for d in CnmvES(fetcher=_StubFetcher()).discover(e)]
    ids2 = [d.doc_id for d in CnmvES(fetcher=_StubFetcher()).discover(e)]
    assert ids1 == ids2, "doc_ids must be deterministic"
    assert all(i.startswith("es-") for i in ids1), "doc_ids must be prefixed 'es-'"


def test_unknown_name_returns_empty():
    """Name with no exact NIF match → [] (no crash, error recorded)."""
    src = CnmvES(fetcher=_StubFetcher())
    docs = src.discover(Entity(lei="L2", name="UNKNOWN CORP NONEXISTENT", country="ES"))
    assert docs == []
    assert any(e["context"] == "resolve-no-match" for e in src.errors)


# ---------------------------------------------------------------------------
# Robustness / error handling
# ---------------------------------------------------------------------------

def test_landing_fetch_failure_returns_empty():
    """If the landing GET fails, discover returns [] and records the error."""

    class _FailGetFetcher:
        def get_text(self, url, **_):
            raise ConnectionError("network down")

        def post_text(self, url, data, **_):
            return _BUSQUEDA_HTML

    src = CnmvES(fetcher=_FailGetFetcher())
    docs = src.discover(Entity(lei="L1", name="IBERDROLA, S.A.", country="ES"))
    assert docs == []
    assert any(e["context"] == "resolve-landing" for e in src.errors)


def test_post_failure_returns_empty():
    """If the search POST fails, discover returns [] and records the error."""

    class _FailPostFetcher:
        def get_text(self, url, **_):
            return _LANDING_HTML

        def post_text(self, url, data, **_):
            raise ConnectionError("network down")

    src = CnmvES(fetcher=_FailPostFetcher())
    docs = src.discover(Entity(lei="L1", name="IBERDROLA, S.A.", country="ES"))
    assert docs == []
    assert any(e["context"] == "resolve-post" for e in src.errors)


def test_register_fetch_failure_records_error_and_continues():
    """If a register GET fails, the error is recorded and other registers proceed."""

    class _PartialFetcher:
        def get_text(self, url, **_):
            if "BusquedaPorEntidad" in url:
                return _LANDING_HTML
            if "resultado-ip" in url:
                raise ConnectionError("IP register down")
            # Other registers return empty pages.
            return _EMPTY_REGISTER_HTML

        def post_text(self, url, data, **_):
            return _BUSQUEDA_HTML

    src = CnmvES(fetcher=_PartialFetcher())
    # Should not raise; returns whatever other registers found (empty here).
    docs = src.discover(Entity(lei="L1", name="IBERDROLA, S.A.", country="ES"))
    # The error must be recorded.
    assert src.errors, "expected at least one error from the failing register"


# ---------------------------------------------------------------------------
# acquire.py wiring
# ---------------------------------------------------------------------------

def test_country_backends_includes_es():
    from company_corpus.eu.acquire import COUNTRY_BACKENDS
    assert "ES" in COUNTRY_BACKENDS
    assert COUNTRY_BACKENDS["ES"] is CnmvES
