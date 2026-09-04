import json
from pathlib import Path
from datetime import date
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.filings_org import FilingsXbrlOrg

FIX = Path(__file__).parent.parent / "fixtures" / "eu"


class _Fetcher:
    def __init__(self, routes): self.routes = routes
    def get_json(self, url, **_):
        for n, v in self.routes.items():
            if n in url:
                return json.loads(v) if isinstance(v, str) else v
        raise RuntimeError(url)


def test_discover_maps_filings_to_documents():
    f = _Fetcher({"/filings": (FIX / "filings_org_entity.json").read_text()})
    src = FilingsXbrlOrg(fetcher=f)
    docs = src.discover(Entity(lei="529900D6BF99LW9R2E68", name="SAP SE", country="DE"))
    assert docs and all(d.doc_type == "annual_report" for d in docs)
    d = docs[0]
    assert d.lei == "529900D6BF99LW9R2E68"
    assert d.source == "filings.xbrl.org"
    assert isinstance(d.period_end, date)
    assert any(fi["url"].startswith("https://filings.xbrl.org") for fi in d.files)


def test_discover_no_lei_returns_empty():
    """When entity.lei is None, discover must return [] without making any fetch."""
    calls = []

    class _TrackingFetcher:
        def get_json(self, url, **_):
            calls.append(url)
            return {"data": []}

    src = FilingsXbrlOrg(fetcher=_TrackingFetcher())
    result = src.discover(Entity(lei=None, name="x", country="EU"))
    assert result == []
    assert calls == [], "no fetch should be attempted when lei is None"


def test_discover_404_returns_empty_and_records_error():
    """When the fetcher raises (e.g. 404), discover must return [] and record an
    error on the backend (never silently drop the failure)."""

    class _ErrorFetcher:
        def get_json(self, url, **_):
            raise RuntimeError("404 Not Found")

    src = FilingsXbrlOrg(fetcher=_ErrorFetcher())
    result = src.discover(Entity(lei="NOTININDEX", name="Ghost Corp", country="EU"))
    assert result == []
    assert src.errors, "a fetch error must be recorded — errors list must be non-empty"


def test_full_page_records_truncation():
    """A full page (>= _PAGE rows) means there may be more -> never silently
    partial: a 'truncated' error is recorded (the page cap is far above any real
    issuer's ESEF report count, so this never fires in practice)."""
    from company_corpus.eu.sources.filings_org import _PAGE
    rows = {"data": [{"id": str(i), "attributes": {"package_url": f"/p/{i}.zip",
                                                   "period_end": "2024-12-31"}}
                     for i in range(_PAGE)]}
    src = FilingsXbrlOrg(fetcher=_Fetcher({"/filings": rows}))
    docs = src.discover(Entity(lei="L1", name="X", country="DE"))
    assert len(docs) == _PAGE
    assert any(e["context"] == "truncated" for e in src.errors)
