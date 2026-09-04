import json
from pathlib import Path
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_fr import InfoFinanciereFR

FIX = Path(__file__).parent.parent / "fixtures" / "eu"


class _Fetcher:
    def __init__(self, routes): self.routes = routes
    def get_json(self, url, **_):
        for n, v in self.routes.items():
            if n in url:
                return json.loads(v) if isinstance(v, str) else v
        raise RuntimeError(url)


def test_fr_discover_maps_records_to_documents():
    f = _Fetcher({"/records": (FIX / "oam_fr_records.json").read_text()})
    src = InfoFinanciereFR(fetcher=f)
    docs = src.discover(Entity(lei="969500P31E3EW0YOR413", name="TotalEnergies SE", country="FR"))
    assert docs
    assert all(d.source == "oam-fr" and d.country == "FR" for d in docs)
    assert all(d.doc_type in __import__("company_corpus.eu.documents", fromlist=["DOC_TYPES"]).DOC_TYPES for d in docs)
    assert all(d.files and d.files[0]["url"].startswith("http") for d in docs)


def test_fr_discover_records_truncation_when_total_count_exceeds_results():
    """When total_count > len(results), the backend must record a 'truncated' error
    so the incompleteness is visible — never silent. The fixture has total_count=3782
    with only 3 results, so truncation fires."""
    f = _Fetcher({"/records": (FIX / "oam_fr_records.json").read_text()})
    src = InfoFinanciereFR(fetcher=f)
    src.discover(Entity(lei="969500P31E3EW0YOR413", name="TotalEnergies SE", country="FR"))
    assert any(e.get("context") == "truncated" for e in src.errors)


def test_fr_paginates_all_records_recent_first():
    """ODS caps limit at 100, so the backend pages by offset until total_count,
    requesting a recent-first order and de-duplicating any overlap."""
    def _page(start, n, total):
        return {"total_count": total, "results": [
            {"uin_idt_uin": i, "url_de_recuperation": f"http://x/{i}.pdf",
             "type_of_information": "Inside information",
             "informationdeposee_inf_dat_emt": "2025-01-01T00:00:00+00:00"}
            for i in range(start, start + n)]}

    class _Paged:
        def __init__(self): self.offsets = []
        def get_json(self, url, **_):
            import re
            off = int(re.search(r"offset=(\d+)", url).group(1))
            self.offsets.append(off)
            assert "order_by=" in url  # recent-first ordering is requested
            return {0: _page(0, 100, 154), 100: _page(100, 54, 154)}.get(
                off, {"total_count": 154, "results": []})

    f = _Paged()
    src = InfoFinanciereFR(fetcher=f)
    docs = src.discover(Entity(lei="L1", name="X", country="FR"))
    assert len(docs) == 154            # both pages, nothing lost
    assert f.offsets[:2] == [0, 100]   # paged by offset
    assert len({d.doc_id for d in docs}) == 154
    assert not src.errors              # fully fetched -> no truncation


def test_fr_discover_queries_lei_and_isins():
    """When entity has ISINs, the where clause must include ISIN predicates (OR).
    Ensures pre-LEI-era records are not silently dropped."""
    captured_urls = []

    class _CaptureFetcher:
        def get_json(self, url, **_):
            captured_urls.append(url)
            return {"total_count": 0, "results": []}

    src = InfoFinanciereFR(fetcher=_CaptureFetcher())
    src.discover(Entity(lei="TESTLEI123", name="Test Co", country="FR",
                        isins=("FR0000123456", "FR0000789012")))
    assert captured_urls, "fetcher should have been called"
    url = captured_urls[0]
    assert "TESTLEI123" in url
    assert "FR0000123456" in url
    assert "FR0000789012" in url


def test_fr_discover_no_url_records_counted_in_errors():
    """Records missing url_de_recuperation must be counted and recorded in errors
    (not silently dropped), so the data loss is detectable. One record has a URL,
    one does not — expect 1 Document and a 'no-url' error entry."""
    fixture = {
        "total_count": 2,
        "results": [
            {
                "uin_idt_uin": "REC001",
                "url_de_recuperation": "https://example.com/doc1.pdf",
                "type_of_information": "Ongoing regulated information",
                "subtype_of_information": "Inside Information",
                "informationdeposee_inf_dat_emt": "2024-01-01T00:00:00+00:00",
            },
            {
                "uin_idt_uin": "REC002",
                "url_de_recuperation": None,  # <-- missing URL
                "type_of_information": "Ongoing regulated information",
                "subtype_of_information": "Inside Information",
                "informationdeposee_inf_dat_emt": "2024-01-02T00:00:00+00:00",
            },
        ],
    }
    f = _Fetcher({"/records": fixture})
    src = InfoFinanciereFR(fetcher=f)
    docs = src.discover(Entity(lei="969500P31E3EW0YOR413", name="TotalEnergies SE", country="FR"))
    assert len(docs) == 1, "only the record with a URL should produce a Document"
    no_url_errors = [e for e in src.errors if e.get("context") == "no-url"]
    assert no_url_errors, "missing-URL drop must be recorded as a 'no-url' error"
    assert "1 records" in no_url_errors[0]["error"]


def test_fr_pagination_without_total_count():
    """Stub returns 2 full pages then a short page with NO 'total_count' field.
    Old code defaulted absent total_count to 0 → len(results) >= 0 always True →
    stopped after page 1.  Fix: absent total_count drives by empty/short page."""
    _PAGE_SIZE = 100

    def _make_record(i):
        return {
            "uin_idt_uin": f"rec-{i}",
            "url_de_recuperation": f"https://example.com/doc{i}.pdf",
            "type_of_information": "Inside information",
            "informationdeposee_inf_dat_emt": "2025-01-01T00:00:00+00:00",
        }

    page0 = [_make_record(i) for i in range(_PAGE_SIZE)]
    page1 = [_make_record(_PAGE_SIZE + i) for i in range(_PAGE_SIZE)]
    page2 = [_make_record(_PAGE_SIZE * 2 + i) for i in range(37)]  # short page

    class _NoTotalFetcher:
        def __init__(self): self.offsets = []
        def get_json(self, url, **_):
            import re
            m = re.search(r"offset=(\d+)", url)
            offset = int(m.group(1)) if m else 0
            self.offsets.append(offset)
            if offset == 0:
                return {"results": page0}             # NO total_count
            if offset == _PAGE_SIZE:
                return {"results": page1}             # NO total_count
            if offset == _PAGE_SIZE * 2:
                return {"results": page2}             # short page → terminates
            return {"results": []}

    f = _NoTotalFetcher()
    src = InfoFinanciereFR(fetcher=f)
    docs = src.discover(Entity(lei="L1", name="X", country="FR"))
    assert len(docs) == _PAGE_SIZE * 2 + 37, (
        f"expected {_PAGE_SIZE * 2 + 37} docs (2 full pages + short page), got {len(docs)}"
    )
    assert not src.errors


def test_fr_discover_malformed_identifier_not_injected():
    """Identifiers containing characters outside ^[A-Z0-9]+$ (e.g. a stray quote)
    must be rejected before building the where-clause — no injection, no crash."""
    captured_urls = []

    class _CaptureFetcher:
        def get_json(self, url, **_):
            captured_urls.append(url)
            return {"total_count": 0, "results": []}

    malformed_lei = 'BAD"INJECTED'   # contains a quote — must NOT appear in query
    safe_isin = "FR0000123456"        # valid, must appear
    src = InfoFinanciereFR(fetcher=_CaptureFetcher())
    src.discover(Entity(lei=malformed_lei, name="Test Co", country="FR",
                        isins=(safe_isin,)))
    assert captured_urls, "fetcher should still be called (safe ISIN present)"
    url = captured_urls[0]
    assert '"' not in url, "malformed identifier must not be interpolated into the query"
    assert safe_isin in url, "valid ISIN must still appear in the query"
