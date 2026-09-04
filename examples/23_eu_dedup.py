"""Cross-backend dedup — the same disclosure from two backends collapses to one.

An ESEF annual report often surfaces from BOTH the national OAM and filings.xbrl.org.
`merge_documents` collapses them pre-download by `(lei, doc_type, period_end, file
names/hashes)`, first-occurrence wins — and backends are listed national-first, so the
more-complete national copy survives. This example is fully OFFLINE: it builds three
hand-made `Document`s (two are the same filing from two sources) and shows the merge.

    ./venv/bin/python examples/23_eu_dedup.py
"""
from __future__ import annotations

from datetime import date

from company_corpus.eu.documents import Document
from company_corpus.eu.dispatcher import merge_documents

LEI = "96950079QAYBTB8V4F22"  # Catana Group SA


def doc(doc_id, source, *, doc_type, sha):
    """A 2024 filing carrying one package file with the given content hash."""
    return Document(
        doc_id=doc_id, lei=LEI, country="FR", doc_type=doc_type,
        period_end=date(2024, 8, 31), published_ts="2024-12-01T00:00:00Z",
        discovered_ts="2026-06-29T00:00:00Z", language="fr", source=source,
        files=[{"name": f"{doc_type}-2024.zip", "kind": "package_url",
                "url": f"https://{source}/{doc_id}.zip", "sha256": sha}],
    )


SAME = "a" * 64  # the AR's ESEF package — byte-identical from both sources
per_backend = [
    # national backend, listed FIRST so it wins first-occurrence dedup
    [doc("amf-ar-2024", "info-financiere.gouv.fr", doc_type="annual_report", sha=SAME),
     doc("amf-hy-2024", "info-financiere.gouv.fr", doc_type="half_year_report", sha="b" * 64)],
    # ESEF aggregator — re-surfaces the SAME annual report (same bytes) -> collapses
    [doc("xbrlorg-ar-2024", "filings.xbrl.org", doc_type="annual_report", sha=SAME)],
]

print("before merge:", sum(len(b) for b in per_backend), "documents across 2 backends")
merged = merge_documents(per_backend)
print("after  merge:", len(merged), "documents")
for d in merged:
    print(f"   {d.doc_type:14} period={d.period_end}  kept from -> {d.source}")
