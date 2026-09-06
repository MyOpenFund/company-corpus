"""Flatten the per-backend discoveries of ONE entity into one document list.

Two things happen here, and only these two:

**A byte-confirmed merge.** A document that already carries a ``sha256`` for one
of its files collapses with another carrying the same hashes on
``(lei, doc_type, period_end, sha256s)`` -- two backends' copies of one
disclosure. Nothing weaker merges: a file *name* is not an identity (``Rob-C3``:
every ``release.pdf`` in an issuer's history used to collapse into one row while
``reconcile()`` reported ``gap: "none"``).

At discovery time no file has been downloaded, so no ``sha256`` exists yet and
this merge is **near-nil by design** -- it fires only on backends that publish a
hash in their listing (``filings.xbrl.org``). The authoritative cross-backend
dedup runs in :mod:`company_corpus.eu.acquire` *after* download, on
``(lei, published-day, sha256)``, where the bytes actually exist. The cost is
some duplicate downloading; the benefit is that no two distinct disclosures are
ever merged on a guess.

**A loud collision.** Everything else keys on ``("doc", doc_id)`` -- and a
``doc_id`` is one raw directory and one manifest path. When two DIFFERENT
documents compute the same one (SIX items sharing ``isin|title|published_ts``, a
BE same-day amendment under one topic id, an ES artefact re-listed at one URL, a
DE publication sharing ``lei|date|title|register``), the loser used to disappear
without a word. It is now reported: through ``on_collision`` when the caller has
an error sink, else as a raised :class:`IdentityCollisionError`.
"""
from __future__ import annotations

from collections.abc import Callable

from ..models import IdentityCollisionError
from .documents import Document

# Called with (kept, dropped) -- the two colliding documents, in listing order.
CollisionSink = Callable[[Document, Document], None]


def merge_documents(per_backend: list[list[Document]],
                    *, on_collision: CollisionSink | None = None) -> list[Document]:
    """Flatten and dedupe the documents discovered by several backends.

    Byte-identical documents collapse (first occurrence wins -- backend order is
    the caller's priority). Two documents that share a computed ``doc_id`` but
    disagree on their files, publication timestamp or title are an identity
    collision, never a dedup: ``on_collision`` is called with ``(kept, dropped)``
    so the caller can degrade that entity's coverage row to ``source-error`` and
    keep going, and without a sink the collision is raised.

    The kept document is still the first-listed one: one ``doc_id`` is one
    directory, so the pair cannot both be acquired. What changes is that the loss
    is on the record instead of silent.
    """
    seen: dict[tuple, Document] = {}
    for docs in per_backend:
        for d in docs:
            k = d.key()
            prev = seen.get(k)
            if prev is None:
                seen[k] = d
                continue
            if d.merges_on_bytes():
                continue  # same hashes: the same bytes, whatever the two URLs say
            if prev.merge_fingerprint() == d.merge_fingerprint():
                continue  # the very same document, listed twice
            if on_collision is None:
                raise IdentityCollisionError(
                    f"doc_id {d.doc_id} is computed by two different documents "
                    f"({prev.source}/{prev.native_id} and {d.source}/{d.native_id}); "
                    "one raw directory cannot hold both, and collapsing them "
                    "silently loses a disclosure.",
                    doc_ids=[d.doc_id])
            on_collision(prev, d)
    return list(seen.values())
