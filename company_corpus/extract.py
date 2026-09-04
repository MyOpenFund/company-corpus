"""Extract clean, RAG-ready plain text from a filing's primary document.

EDGAR primary documents are HTML, inline-XBRL (XHTML), or occasionally plain
text. This module strips markup and normalizes whitespace so the downstream RAG
chunker sees readable prose rather than tags or boilerplate.
"""

from __future__ import annotations

import re
import warnings

from bs4 import BeautifulSoup

try:  # bs4 emits this when an XML doc is parsed by the HTML parser (e.g. Form 4 XML)
    from bs4 import XMLParsedAsHTMLWarning
except ImportError:  # pragma: no cover - older bs4
    XMLParsedAsHTMLWarning = None


def _soup(html: str) -> BeautifulSoup:
    with warnings.catch_warnings():
        if XMLParsedAsHTMLWarning is not None:
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        return BeautifulSoup(html, "lxml")

_WS_RUN = re.compile(r"[ \t\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_whitespace(text: str) -> str:
    """Collapse runs of spaces/tabs and excess blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RUN.sub(" ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def html_to_text(html: str) -> str:
    """Render HTML / inline-XBRL to readable text (scripts/styles removed)."""
    soup = _soup(html)
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    # Inline-XBRL hidden facts live in <ix:header>; drop them if present.
    for hidden in soup.find_all(attrs={"style": re.compile(r"display:\s*none", re.I)}):
        hidden.decompose()
    text = soup.get_text(separator="\n")
    return normalize_whitespace(text)


def looks_like_html(content: str, filename: str = "") -> bool:
    fn = filename.lower()
    if fn.endswith((".htm", ".html", ".xml", ".xsd")):
        return True
    head = content[:2000].lower()
    return "<html" in head or "<!doctype html" in head or "<ix:" in head or "<xbrl" in head


def clean_text(content: str, filename: str = "") -> str:
    """Return normalized plain text for a primary document of any supported type."""
    if looks_like_html(content, filename):
        return html_to_text(content)
    return normalize_whitespace(content)
