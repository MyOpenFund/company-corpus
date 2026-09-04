"""Coverage reconciliation: resolved universe vs documents found.

Surfaces gaps explicitly (an entity with no documents, or that never resolved) so
the corpus is detectably incomplete rather than silently partial -- mirrors the US
completeness reporting.
"""
from __future__ import annotations

from collections import defaultdict

from .documents import Document
from .entities import Entity


def reconcile(entities: list[Entity], documents: list[Document]) -> list[dict]:
    by_lei: dict[str, list[Document]] = defaultdict(list)
    for d in documents:
        if d.lei:
            by_lei[d.lei].append(d)
    rows = []
    for e in entities:
        docs = by_lei.get(e.lei or "", [])
        if e.resolution == "unresolved" or not e.lei:
            gap = "unresolved-entity"
        elif not docs:
            gap = "no-documents"
        else:
            gap = "none"
        rows.append({
            "lei": e.lei, "name": e.name, "country": e.country, "resolution": e.resolution,
            "doc_count": len(docs), "doc_types": sorted({d.doc_type for d in docs}), "gap": gap,
        })
    return rows
