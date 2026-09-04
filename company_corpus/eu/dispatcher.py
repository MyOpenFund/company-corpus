from __future__ import annotations
from .documents import Document


def merge_documents(per_backend: list[list[Document]]) -> list[Document]:
    """Flatten and dedupe documents discovered by multiple backends.

    Two documents collapse when they share (lei, doc_type, period_end, file hashes);
    the first occurrence wins (backend order = caller's priority).
    """
    seen: dict[tuple, Document] = {}
    for docs in per_backend:
        for d in docs:
            seen.setdefault(d.key(), d)
    return list(seen.values())
