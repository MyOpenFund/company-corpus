import builtins
import json
import pytest
from company_corpus.config import Config
from company_corpus.eu.entities import Entity
from company_corpus.eu import arelle_esef
from company_corpus.eu import financials as eufin


def _write_manifest(cfg, lei, doc_id, files):
    p = cfg.data_dir / "manifest" / lei / f"{doc_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"doc_id": doc_id, "lei": lei, "doc_type": "annual_report",
                             "period_end": "2024-12-31", "published_ts": "2025-04-01 00:00:00",
                             "source": "oam-it", "files": files}))


def test_arelle_facts_for_entity_discovers_esef_and_unions(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    # one ESEF file + one non-ESEF (must be ignored)
    _write_manifest(cfg, "LEI1", "d1", [
        {"name": "r.zip", "kind": "esef", "path": "raw/LEI1/ESEF-AR/2024/d1/r.zip"},
        {"name": "r.pdf", "kind": "document", "path": "raw/LEI1/ESEF-AR/2024/d1/r.pdf"}])
    seen = {}
    def fake_bridge(zip_path, **kw):
        seen["zip"] = zip_path
        return {"facts": {"f": {"value": "100", "dimensions": {
            "concept": "ifrs-full:Revenue", "entity": "x", "unit": "iso4217:EUR",
            "period": "2024-01-01T00:00:00/2025-01-01T00:00:00"}}}}
    monkeypatch.setattr(eufin, "oim_from_esef_zip", fake_bridge)
    flat = eufin.arelle_facts_for_entity(Entity(lei="LEI1", name="X", country="IT"), config=cfg)
    assert seen["zip"].endswith("raw/LEI1/ESEF-AR/2024/d1/r.zip")    # only the esef file
    assert flat["Revenue"][0]["val"] == 100
    assert flat["Revenue"][0]["filed"] == "2025-04-01"               # published_ts truncated [:10]


def test_build_eu_financials_use_arelle_fills_the_gap(tmp_path, monkeypatch):
    from company_corpus.eu.financials import build_eu_financials
    monkeypatch.setattr("company_corpus.eu.financials.resolve_entities",
                        lambda specs, **kw: [Entity(lei="LEI1", name="X", country="IT")])
    monkeypatch.setattr("company_corpus.eu.financials.facts_for_entity", lambda e, **kw: {})  # Tier A empty
    pt = {"val": 100, "end": "2024-12-31", "start": "2024-01-01", "unit": "EUR",
          "tag": "Revenue", "label": "Revenue", "filed": "2025-04-01", "form": "annual_report", "accn": "d1"}
    monkeypatch.setattr("company_corpus.eu.financials.arelle_facts_for_entity",
                        lambda e, **kw: {"Revenue": [pt]})
    cfg = Config(data_dir=tmp_path)
    rep = build_eu_financials([{"lei": "LEI1"}], fetcher=None, config=cfg, write=True, use_arelle=True)
    assert rep["with_financials"] == 1
    rows = [json.loads(x) for x in (tmp_path / "financials_eu" / "LEI1.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "reported" and r["concept"] == "revenue" and r["value"] == 100 for r in rows)


def test_oim_from_esef_zip_raises_clear_error_when_arelle_missing(monkeypatch, tmp_path):
    # Simulate Arelle not installed: make `import arelle...` fail inside the function.
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name.startswith("arelle"):
            raise ImportError("no arelle")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    z = tmp_path / "x.zip"; z.write_bytes(b"PK\x03\x04")
    with pytest.raises(ImportError, match="eu-financials"):
        arelle_esef.oim_from_esef_zip(str(z))


def test_tls_cert_check_stays_on_when_verify_tls_true(monkeypatch, tmp_path):
    """When config.verify_tls=True (default), noCertificateCheck must NOT be set
    to True on the Arelle controller — fix for unconditional TLS-off."""
    import sys
    import types
    import zipfile
    import io
    from company_corpus.config import Config

    # Write a minimal ESEF zip. The inner-path check is
    # ``n.endswith(".xhtml") and "/reports/" in n`` — the path must have a
    # parent component (e.g. "issuer/reports/a.xhtml") for "/reports/" to match.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("issuer/reports/a.xhtml", "<html/>")
    zip_path = str(tmp_path / "test.zip")
    (tmp_path / "test.zip").write_bytes(buf.getvalue())

    # Inject a stub arelle package so the `from arelle import Cntlr` inside
    # oim_from_esef_zip doesn't raise ImportError (arelle not installed here).
    fake_arelle_cntlr_mod = types.ModuleType("arelle.Cntlr")
    fake_arelle_mod = types.ModuleType("arelle")
    fake_arelle_mod.Cntlr = fake_arelle_cntlr_mod
    monkeypatch.setitem(sys.modules, "arelle", fake_arelle_mod)
    monkeypatch.setitem(sys.modules, "arelle.Cntlr", fake_arelle_cntlr_mod)

    class _WebCache:
        """Mutable cache stub.  noCertificateCheck starts False (instance attr)."""
        def __init__(self):
            self.noCertificateCheck = False

    class _FakeCntrl:
        """Minimal Arelle controller stub: webCache + modelManager.load → None."""
        def close(self): pass
        class _MM:
            @staticmethod
            def load(p): return None
        modelManager = _MM()

    cfg_on = Config(data_dir=tmp_path, verify_tls=True)
    cfg_off = Config(data_dir=tmp_path, verify_tls=False)

    # Case 1: verify_tls=True — noCertificateCheck must remain False.
    cntlr_on = _FakeCntrl()
    cntlr_on.webCache = _WebCache()         # instance attribute; starts False
    try:
        arelle_esef.oim_from_esef_zip(zip_path, cntlr=cntlr_on, config=cfg_on)
    except Exception:
        pass  # ValueError("no facts") expected — model.load() returns None
    assert cntlr_on.webCache.noCertificateCheck is False, (
        "noCertificateCheck must remain False when verify_tls=True"
    )

    # Case 2: verify_tls=False — noCertificateCheck must be set to True.
    cntlr_off = _FakeCntrl()
    cntlr_off.webCache = _WebCache()        # separate instance; starts False
    try:
        arelle_esef.oim_from_esef_zip(zip_path, cntlr=cntlr_off, config=cfg_off)
    except Exception:
        pass
    assert cntlr_off.webCache.noCertificateCheck is True, (
        "noCertificateCheck must be True when verify_tls=False"
    )
