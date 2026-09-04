from datetime import date
from company_corpus.eu.entities import Entity
from company_corpus.eu.documents import Document
from company_corpus.eu.reconcile import reconcile


def _doc(lei, dt):
    return Document(doc_id="d", lei=lei, country="DE", doc_type=dt, period_end=date(2023, 12, 31),
                    published_ts=None, discovered_ts="x", language=None, source="oam-de",
                    files=[{"name": "f", "sha256": "h"}], native_meta={})


def test_reconcile_flags_gaps():
    ents = [Entity("L1", "A", "DE", resolution="lei"),
            Entity("L2", "B", "DE", resolution="lei"),
            Entity(None, "C", "FR", resolution="unresolved")]
    docs = [_doc("L1", "annual_report"), _doc("L1", "inside_information")]
    rows = {r["lei"] or r["name"]: r for r in reconcile(ents, docs)}
    assert rows["L1"]["doc_count"] == 2 and rows["L1"]["gap"] == "none"
    assert rows["L1"]["doc_types"] == ["annual_report", "inside_information"]
    assert rows["L2"]["gap"] == "no-documents"
    assert rows["C"]["gap"] == "unresolved-entity"
