"""The EU pillar's document model and its one identity rule.

Each backend used to mint its own ``doc_id`` string, and three of them minted it
from values that change between runs (a page offset, ``len(files)``, the wall
clock), so the same disclosure acquired a new id -- and a new raw directory and a
new manifest -- on the next night. Identity is now computed in one place from
``(source, country, native_id)``, where ``native_id`` is the *source's own*
stable handle on the document.

There is no compatibility shim: a document acquired before this change keeps its
old directory under ``data/raw/`` and its old ``manifest/<lei>/<doc_id>.json``,
and nothing will ever look at them again. Re-run ``eu-acquire`` and delete the
orphans, or keep both -- but do not expect the two spellings to converge.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date

DOC_TYPES = ("annual_report", "half_year_report", "interim_statement",
             "inside_information", "holding_notification", "prospectus",
             "governance", "other")

DOC_FAMILY = {"annual_report": "ESEF-AR", "half_year_report": "HY",
              "interim_statement": "IMS", "inside_information": "MAR",
              "holding_notification": "TVR", "prospectus": "PROSPECTUS",
              "governance": "GOV", "other": "OTHER"}


def source_key(*parts) -> str:
    """Join a source's OWN key parts into a native id, or return ``""``.

    ``str(x or "")`` throws away a legitimate ``0`` — several registers number
    their rows from zero — and turns a missing key into the literal ``"None"``,
    which then reads like a real id for the rest of the run. Only ``None`` or
    blank text means "this source gave us no key"; the empty return is refused
    by :meth:`Document.__post_init__`, loudly, at the call site.
    """
    if any(p is None or not str(p).strip() for p in parts):
        return ""
    return "-".join(str(p).strip() for p in parts)


def stable_native_id(*parts) -> str:
    """A deterministic native id from whatever stable parts a source offers.

    For listings with no id of their own. Raises when every part is empty --
    minting an id from the clock or from ``len(docs)`` produces a *different*
    document every night, which is worse than a recorded gap.
    """
    usable = [str(p).strip() for p in parts if str(p or "").strip()]
    if not usable:
        raise ValueError("no stable part available for a native id")
    return hashlib.sha1("|".join(usable).encode("utf-8")).hexdigest()[:16]


@dataclass
class Document:
    native_id: str          # the SOURCE's own stable id for this document
    lei: str | None
    country: str
    doc_type: str
    period_end: date | None
    published_ts: str | None
    discovered_ts: str
    language: str | None
    source: str
    files: list[dict] = field(default_factory=list)
    native_meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.native_id).strip():
            raise ValueError("Document.native_id is required and must be stable")

    @property
    def doc_id(self) -> str:
        """Computed identity: sha1(source|country|native_id), 16 hex chars.

        Only the three components that cannot drift between two runs over the
        same source. Not the title (sources correct them), not ``discovered_ts``
        or any other clock reading, not the document's position in a result page.
        Producers hand over a ``native_id`` and never an id; there is exactly one
        rule and one place for it.
        """
        basis = f"{self.source}|{self.country}|{self.native_id}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    def key(self) -> tuple:
        """Cross-backend dedup key.

        When the document has at least one file whose bytes we have already
        hashed, two backends' copies of the same disclosure collapse on
        ``(lei, doc_type, period_end, sha256s)``. Anything weaker is not an
        identity: the old key fell back to the *file name*, so an issuer's whole
        notification history -- SE flaggings with no file at all, and every FI /
        NO / Euronext release whose attachment is called ``release.pdf`` --
        collapsed into one row while ``reconcile()`` cheerfully reported
        ``gap: "none"`` (Rob-C3). A document with no hashed file is therefore
        only ever equal to itself.

        Discovery has no sha256 yet, so in practice this key only merges
        already-downloaded documents; ``acquire`` does the authoritative
        byte-level merge after download, where the hashes actually exist.
        """
        hashes = tuple(sorted(h for h in (f.get("sha256") for f in self.files) if h))
        if not hashes:
            return ("doc", self.doc_id)
        return (self.lei, self.doc_type, self.period_end, hashes)
