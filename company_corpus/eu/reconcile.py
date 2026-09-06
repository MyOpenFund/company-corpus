"""Coverage reconciliation: resolved universe vs documents found.

Surfaces gaps explicitly (an entity with no documents, or that never resolved) so
the corpus is detectably incomplete rather than silently partial -- mirrors the US
completeness reporting.
"""
from __future__ import annotations

from collections import defaultdict

from .documents import Document
from .entities import Entity


def reconcile(
    entities: list[Entity],
    documents: list[Document],
    errors: dict[str, str] | None = None,
) -> list[dict]:
    """One coverage row per entity; ``gap`` says WHY an entity has no documents.

    ``errors`` maps a LEI to the failure its backend(s) reported during
    discovery. Such an entity is a ``source-error`` (with the message in
    ``error``), never ``no-documents``: "the OAM was unreachable" and "the issuer
    published nothing" are different facts, and only the second is a property of
    the issuer.

    A recorded failure outranks the documents that DID arrive, too: an issuer
    whose OAM died mid-listing, or one of whose documents lost an identity
    collision, has a listing we already know to be short, and reporting that as
    ``none`` is exactly the silent partial this module exists to prevent.
    """
    errors = errors or {}
    by_lei: dict[str, list[Document]] = defaultdict(list)
    for d in documents:
        if d.lei:
            by_lei[d.lei].append(d)
    rows = []
    for e in entities:
        docs = by_lei.get(e.lei or "", [])
        error = errors.get(e.lei or "")
        if e.resolution == "unresolved" or not e.lei:
            gap = "unresolved-entity"
        elif error:
            # A recorded discovery failure outranks the documents that DID
            # arrive: an issuer whose OAM died mid-listing, or one of whose
            # documents lost an identity collision, has a listing we know to be
            # short. Calling that ``none`` is the silent partial this module
            # exists to prevent.
            gap = "source-error"
        elif not docs:
            gap = "no-documents"
        else:
            gap = "none"
        row = {
            "lei": e.lei, "name": e.name, "country": e.country, "resolution": e.resolution,
            "doc_count": len(docs), "doc_types": sorted({d.doc_type for d in docs}), "gap": gap,
        }
        if gap == "source-error":
            row["error"] = error
        rows.append(row)
    return rows
