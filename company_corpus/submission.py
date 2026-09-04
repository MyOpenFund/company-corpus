"""Parse the EDGAR complete-submission ``.txt`` into its constituent documents.

An EDGAR complete submission is an SGML wrapper around one or more documents:

    <SEC-DOCUMENT>0000320193-24-000123.txt : 20241101
    <SEC-HEADER>...</SEC-HEADER>
    <DOCUMENT>
    <TYPE>10-K
    <SEQUENCE>1
    <FILENAME>aapl-20240928.htm
    <DESCRIPTION>10-K
    <TEXT>
    ...primary document (HTML / iXBRL / text)...
    </TEXT>
    </DOCUMENT>
    <DOCUMENT> ...exhibit... </DOCUMENT>
    ...

The header pseudo-tags (``<TYPE>``, ``<SEQUENCE>``, ``<FILENAME>``,
``<DESCRIPTION>``) are unclosed and sit one-per-line; the body is delimited by
``<TEXT>`` / ``</TEXT>``. This module extracts those documents and picks the
primary one, so downstream extraction works on the report itself rather than the
whole exhibit bundle.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_DOCUMENT_RE = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.DOTALL | re.IGNORECASE)
_TEXT_RE = re.compile(r"<TEXT>(.*?)</TEXT>", re.DOTALL | re.IGNORECASE)


def _tag(block: str, name: str) -> str:
    """Read an unclosed one-line SGML header tag value, e.g. ``<TYPE>10-K``."""
    m = re.search(rf"<{name}>[ \t]*(.*)", block, re.IGNORECASE)
    return m.group(1).strip() if m else ""


@dataclass
class SubmissionDocument:
    """One document inside a complete submission."""

    type: str
    sequence: str
    filename: str
    description: str
    text: str

    @property
    def is_text_like(self) -> bool:
        """True for HTML/XML/plain-text bodies (not uuencoded binaries)."""
        fn = self.filename.lower()
        if fn.endswith((".htm", ".html", ".xml", ".txt", ".xsd")):
            return True
        # Binary docs (GRAPHIC/ZIP/PDF/EX-101.*) carry uuencoded payloads.
        return self.type.upper() not in {"GRAPHIC", "ZIP", "PDF", "EX-101.INS"} and not fn.endswith(
            (".jpg", ".gif", ".png", ".pdf", ".zip")
        )


def parse_submission(raw: str) -> list[SubmissionDocument]:
    """Split a complete submission into its ``<DOCUMENT>`` parts."""
    docs: list[SubmissionDocument] = []
    for block in _DOCUMENT_RE.findall(raw):
        text_match = _TEXT_RE.search(block)
        text = text_match.group(1).strip("\n") if text_match else ""
        docs.append(
            SubmissionDocument(
                type=_tag(block, "TYPE"),
                sequence=_tag(block, "SEQUENCE"),
                filename=_tag(block, "FILENAME"),
                description=_tag(block, "DESCRIPTION"),
                text=text,
            )
        )
    return docs


def select_primary(
    docs: Sequence[SubmissionDocument],
    *,
    primary_filename: str = "",
    sec_form: str = "",
) -> SubmissionDocument | None:
    """Pick the primary document.

    Preference order: exact filename match (from the submissions API) →
    ``<TYPE>`` equal to the filing form → ``<SEQUENCE>`` 1 → first text-like
    document → first document.
    """
    if not docs:
        return None

    if primary_filename:
        for d in docs:
            if d.filename and d.filename.lower() == primary_filename.lower():
                return d

    if sec_form:
        for d in docs:
            if d.type and d.type.upper() == sec_form.upper():
                return d

    for d in docs:
        if d.sequence == "1":
            return d

    for d in docs:
        if d.is_text_like and d.text:
            return d

    return docs[0]


def filename_from_url(url: str) -> str:
    """Last path segment of a URL (the primary document filename)."""
    return url.rsplit("/", 1)[-1] if url else ""
