from datetime import date
from company_corpus.eu.documents import Document, DOC_FAMILY
from company_corpus.eu.dispatcher import merge_documents


def _doc(source, sha):
    return Document(doc_id=f"{source}-1", lei="L1", country="DE", doc_type="annual_report",
                    period_end=date(2023, 12, 31), published_ts="2024-03-01", discovered_ts="x",
                    language="de", source=source, files=[{"name": "r.html", "sha256": sha}],
                    native_meta={})


def test_doc_family_maps_types():
    assert DOC_FAMILY["annual_report"] == "ESEF-AR"
    assert DOC_FAMILY["half_year_report"] == "HY"


def test_merge_dedupes_same_document_across_backends():
    a = _doc("oam-de", "abc")
    b = _doc("filings.xbrl.org", "abc")  # same (lei, type, period, file-hash) -> dup
    c = _doc("oam-de", "different")
    merged = merge_documents([[a, c], [b]])
    assert len(merged) == 2  # a/b collapsed, c kept
