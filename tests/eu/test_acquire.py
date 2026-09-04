import pytest
from datetime import date
from company_corpus.config import Config
from company_corpus.eu import acquire as acq
from company_corpus.eu.documents import Document
from company_corpus.eu.entities import Entity


def test_acquire_resolves_dispatches_merges_and_reconciles(monkeypatch, tmp_path):
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    ent = Entity("L1", "SAP SE", "DE", resolution="lei")
    monkeypatch.setattr(acq, "resolve_entities", lambda specs, *, fetcher: [ent])

    class _Backend:
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e):
            return [Document("de-1", "L1", "DE", "annual_report", date(2023, 12, 31),
                             None, "x", "de", "oam-de", [{"name": "r", "sha256": "h"}], {})]
    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"DE": _Backend})
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _Backend)

    summary = acq.acquire([{"lei": "L1"}], fetcher=object(), config=cfg, download=False)
    assert summary["entities"] == 1
    assert summary["documents"] == 1  # both backends return the same doc -> deduped to 1
    assert (cfg.data_dir / "reports" / "eu_coverage.jsonl").exists()


def test_euronext_appended_after_national_for_its_markets(monkeypatch, tmp_path):
    """For a Euronext market, acquire runs the national backend BEFORE Euronext
    (so the national doc wins dedup ties) — asserted behaviorally, not by grep."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    ent = Entity("L1", "ASML", "NL", resolution="lei")  # NL has a national backend
    monkeypatch.setattr(acq, "resolve_entities", lambda specs, *, fetcher: [ent])

    calls = []

    def _mk(tag):
        class _B:
            def __init__(self, *a, **k): self.errors = []
            def discover(self, e): calls.append(tag); return []
        return _B

    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"NL": _mk("national")})
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _mk("filings"))
    monkeypatch.setattr(acq, "EuronextSource", _mk("euronext"))

    acq.acquire([{"lei": "L1"}], fetcher=object(), config=cfg, download=False)
    assert "euronext" in calls, "Euronext must be invoked for an NL (XAMS) entity"
    assert calls.index("national") < calls.index("euronext"), "national must run first"


def test_has_oslo_notice_signal():
    from company_corpus.eu.acquire import _has_oslo_notice
    osl = Document("e1", "L", "CY", "other", None, "x", "x", "en", "euronext", [],
                   {"notice_number": "OSL_20260101_1_EUR"})
    ams = Document("e2", "L", "NL", "other", None, "x", "x", "en", "euronext", [],
                   {"notice_number": "AMS_20260101_1_EUR"})
    assert _has_oslo_notice([[osl]]) is True
    assert _has_oslo_notice([[ams]]) is False
    assert _has_oslo_notice([[]]) is False


def test_oslo_corroboration_invokes_newsweb_only_when_listed(monkeypatch, tmp_path):
    """A non-NO uncovered issuer gets the rich Oslo NewsWeb pass ONLY when the
    Euronext probe returned an Oslo (OSL_) notice — corroboration, no name guess."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    ent = Entity("L1", "Frontline plc", "CY", resolution="isin")  # CY: no backend
    monkeypatch.setattr(acq, "resolve_entities", lambda specs, *, fetcher: [ent])
    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {})

    class _Noop:
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return []
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _Noop)

    osl_doc = Document("euronext-1", "L1", "CY", "other", None, "2026-01-01", "x",
                       "en", "euronext", [], {"notice_number": "OSL_20260101_1_EUR"})
    called = {}

    class _NewsWeb:
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): called["name"] = e.name; return []
    monkeypatch.setattr(acq, "NewsWebNO", _NewsWeb)

    # 1. Euronext returns an Oslo notice -> NewsWeb invoked.
    class _EurOslo:
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return [osl_doc]
    monkeypatch.setattr(acq, "EuronextSource", _EurOslo)
    acq.acquire([{"isin": "X"}], fetcher=object(), config=cfg, download=False)
    assert called.get("name") == "Frontline plc"

    # 2. Euronext returns a non-Oslo (Amsterdam) notice -> NewsWeb NOT invoked.
    called.clear()
    ams_doc = Document("euronext-2", "L1", "CY", "other", None, "2026-01-01", "x",
                       "en", "euronext", [], {"notice_number": "AMS_20260101_1_EUR"})

    class _EurAms:
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return [ams_doc]
    monkeypatch.setattr(acq, "EuronextSource", _EurAms)
    acq.acquire([{"isin": "X"}], fetcher=object(), config=cfg, download=False)
    assert "name" not in called


def test_uncovered_country_gets_euronext_listing_fallback(monkeypatch, tmp_path):
    """An entity whose home country has no backend (e.g. Bermuda) gets the
    Euronext listing fallback (force_mic set), so a venue-listed offshore issuer
    is still covered."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    ent = Entity("L1", "2020 Bulkers Ltd", "BM", resolution="isin")  # BM: no backend
    monkeypatch.setattr(acq, "resolve_entities", lambda specs, *, fetcher: [ent])
    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {})  # nothing for BM

    class _Noop:
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return []
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _Noop)

    captured = {}

    class _Euronext:
        def __init__(self, *a, force_mic=None, **k):
            captured["force_mic"] = force_mic
            self.errors = []
        def discover(self, e): return []
    monkeypatch.setattr(acq, "EuronextSource", _Euronext)

    acq.acquire([{"isin": "BMG9156K1018"}], fetcher=object(), config=cfg, download=False)
    assert captured.get("force_mic") == acq._LISTING_MIC  # listing mode engaged


def test_acquire_dedupes_byte_identical_across_backends(monkeypatch, tmp_path):
    """Two backends emit the same disclosure (same lei/day/type) under different
    file names; once downloaded, the identical sha256 confirms the duplicate and
    the second (lower-priority) copy is dropped from the corpus."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    ent = Entity("L1", "EDP", "PT", resolution="lei")
    monkeypatch.setattr(acq, "resolve_entities", lambda specs, *, fetcher: [ent])

    # Same content, different file names + doc_type -> survives the file-name merge.
    nat = Document("nat-1", "L1", "PT", "annual_report", None, "2026-04-23", "x", "en",
                   "oam-pt", [{"name": "afm.pdf", "url": "u1", "kind": "document"}], {})
    eur = Document("eur-9", "L1", "PT", "other", None, "2026-04-23", "x", "en",
                   "euronext", [{"name": "euronext-9.pdf", "url": "u2", "kind": "document"}], {})

    class _NatBackend:
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return [nat]

    class _EurBackend:
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return [eur]

    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"PT": _NatBackend})
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _EurBackend)

    # Both files download to the SAME bytes (identical sha256).
    def _fake_download(doc, *, fetcher, config):
        f = doc.files[0]
        return {"doc_id": doc.doc_id, "lei": doc.lei,
                "files": [{"name": f["name"], "sha256": "IDENTICAL",
                           "path": f"raw/{doc.doc_id}/{f['name']}"}]}
    monkeypatch.setattr(acq, "download_document", _fake_download)

    summary = acq.acquire([{"lei": "L1"}], fetcher=object(), config=cfg, download=True)
    assert summary["deduped_by_bytes"] == 1
    assert summary["documents"] == 1        # only the first (national) doc kept
    assert summary["manifests"] == 1


def test_acquire_surfaces_download_errors(monkeypatch, tmp_path):
    """acquire() must record per-file download failures in summary['errors']
    and expose them via download_errors count — never silently drop them."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    ent = Entity("L2", "Test Corp", "FR", resolution="lei")
    monkeypatch.setattr(acq, "resolve_entities", lambda specs, *, fetcher: [ent])

    doc = Document(
        "fr-bad", "L2", "FR", "annual_report", date(2023, 12, 31),
        None, "x", "fr", "oam-fr",
        [{"name": "bad.pdf", "url": "https://example.com/bad.pdf", "kind": "document"}],
        {}
    )

    class _DiscoverBackend:
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return [doc]

    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"FR": _DiscoverBackend})
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _DiscoverBackend)

    # Fake fetcher whose download always raises
    class _FailFetcher:
        def download(self, url, dest): raise RuntimeError("network unreachable")

    # Monkeypatch download_document to simulate a file-level error in the manifest
    def _fake_download(doc, *, fetcher, config):
        return {
            "doc_id": doc.doc_id,
            "files": [{"name": "bad.pdf", "url": "https://example.com/bad.pdf",
                       "error": "network unreachable"}],
        }

    monkeypatch.setattr(acq, "download_document", _fake_download)

    summary = acq.acquire([{"lei": "L2"}], fetcher=_FailFetcher(), config=cfg, download=True)

    assert summary["download_errors"] >= 1
    assert any(e.get("context") == "download" for e in summary["errors"])


def _noop_backend(name=None):
    class _B:
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return []
    if name:
        _B.name = name
    return _B


def test_acquire_write_false_writes_nothing(monkeypatch, tmp_path):
    """The CLI's dry run: discovery only, no entity index, no coverage file."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    ent = Entity("L1", "SAP SE", "DE", resolution="lei")
    monkeypatch.setattr(acq, "resolve_entities", lambda specs, *, fetcher: [ent])
    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"DE": _noop_backend()})
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _noop_backend())

    summary = acq.acquire([{"lei": "L1"}], fetcher=object(), config=cfg,
                          download=False, write=False)
    assert summary["coverage_path"] is None
    assert not (cfg.data_dir / "reports" / "eu_coverage.jsonl").exists()
    assert not (cfg.data_dir / "universe" / "eu_entities.jsonl").exists()


def test_acquire_download_requires_write(monkeypatch, tmp_path):
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    monkeypatch.setattr(acq, "resolve_entities", lambda specs, *, fetcher: [])
    with pytest.raises(ValueError):
        acq.acquire([{"lei": "L1"}], fetcher=object(), config=cfg, download=True, write=False)


def test_acquire_reports_per_backend_counts(monkeypatch, tmp_path):
    """`sources` says, per backend name, how many entities it was asked about,
    how many kept documents it contributed and how many errors it raised or
    recorded — the run report's per-authority rows come from here. A discover
    failure is tagged with the backend's name, never a generic 'acquire'."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    ents = [Entity("L1", "SAP SE", "DE", resolution="lei"),
            Entity("L2", "Siemens", "DE", resolution="lei"),
            Entity(None, "ghost", "", resolution="unresolved")]
    monkeypatch.setattr(acq, "resolve_entities", lambda specs, *, fetcher: ents)

    class _National:
        name = "oam-de"
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e):
            if e.lei == "L2":
                raise RuntimeError("Bundesanzeiger down")
            self.errors.append({"source": self.name, "context": "list", "url": "u",
                                "error": "404"})
            return [Document("de-1", "L1", "DE", "annual_report", date(2023, 12, 31),
                             None, "x", "de", "oam-de", [{"name": "r", "sha256": "h"}], {})]

    class _Filings:
        name = "filings.xbrl.org"
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e):
            return [Document(f"f-{e.lei}", e.lei, "DE", "annual_report", date(2022, 12, 31),
                             None, "x", "en", "filings.xbrl.org",
                             [{"name": "q", "sha256": "k"}], {})]

    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"DE": _National})
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _Filings)

    summary = acq.acquire([{"lei": "L1"}, {"lei": "L2"}, {"isin": "X"}],
                          fetcher=object(), config=cfg, download=False, write=False)
    assert summary["entities"] == 3 and summary["unresolved"] == 1
    # The input specs that resolved to no LEI, in input order (one Entity per spec).
    assert summary["unresolved_specs"] == [{"isin": "X"}]
    assert summary["sources"] == {
        "oam-de": {"entities": 2, "documents": 1, "errors": 2, "not_indexed": 0},
        "filings.xbrl.org": {"entities": 2, "documents": 2, "errors": 0, "not_indexed": 0},
    }
    dead = [e for e in summary["errors"] if e.get("context") == "discover"]
    assert dead == [{"source": "oam-de", "context": "discover", "entity": "L2",
                     "error": "Bundesanzeiger down"}]
    assert len(summary["errors"]) == 2


def test_acquire_counts_aggregator_not_indexed_as_a_note(monkeypatch, tmp_path):
    """A filings.xbrl.org 404 (issuer not indexed) is a note on the backend's
    row, not an error: `not_indexed` counts it, `errors` stays 0."""
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    monkeypatch.setattr(acq, "resolve_entities",
                        lambda specs, *, fetcher: [Entity("L1", "X", "DE", resolution="lei")])

    class _Empty:
        name = "oam-de"
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return []

    class _NotIndexed:
        name = "filings.xbrl.org"
        def __init__(self, *a, **k):
            self.errors = []
            self.notes = []
        def discover(self, e):
            self.notes.append({"source": self.name, "context": "not-indexed", "url": "u",
                               "note": "HTTP 404"})
            return []

    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"DE": _Empty})
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _NotIndexed)
    summary = acq.acquire([{"lei": "L1"}], fetcher=object(), config=cfg,
                          download=False, write=False)
    assert summary["errors"] == []
    assert summary["sources"]["filings.xbrl.org"]["not_indexed"] == 1
    assert summary["sources"]["filings.xbrl.org"]["errors"] == 0
    assert summary["sources"]["oam-de"]["not_indexed"] == 0
