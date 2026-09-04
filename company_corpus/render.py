"""Headless-Chrome HTML -> PDF rendering.

Parallels cb_corpus's ``htmlpdf.py`` / ``convert-html``. SEC primary documents
are HTML / inline-XBRL; this renders them to a portable, paginated PDF that a
human can read and that the RAG can ingest with page-anchored citations.

Rendering is intentionally a **separate batch** (its own CLI command), run over a
chosen subset (e.g. the curated tier), because headless Chrome is slow and PDFs
are large — we never render millions of small filings.

Chrome/Chromium must be installed where this runs (it is not bundled). The
binary is located via ``COMPANY_CORPUS_CHROME`` or the PATH. The renderer is a
plain ``Callable[[Path, Path], None]`` so it can be swapped (e.g. in tests).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

# A renderer turns an input document (HTML/text) into a PDF at the given path.
Renderer = Callable[[Path, Path], None]

CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)


def find_chrome(explicit: str | None = None) -> str | None:
    """Locate a Chrome/Chromium binary.

    Order: explicit arg, ``COMPANY_CORPUS_CHROME`` env var, then PATH.
    Returns the resolved path or ``None`` if not found.
    """
    for candidate in (explicit, os.environ.get("COMPANY_CORPUS_CHROME")):
        if candidate:
            resolved = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
            if resolved:
                return resolved
    for name in CHROME_CANDIDATES:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def make_chrome_renderer(chrome: str | None = None, *, timeout: float = 120.0) -> Renderer:
    """Build a renderer that drives headless Chrome's ``--print-to-pdf``.

    Raises ``RuntimeError`` immediately if no Chrome binary can be found, so the
    failure is reported up front rather than per-document.
    """
    binary = find_chrome(chrome)
    if not binary:
        raise RuntimeError(
            "no Chrome/Chromium found; install it or set COMPANY_CORPUS_CHROME "
            "to the binary path"
        )

    def _render(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            binary,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            f"--print-to-pdf={dst}",
            src.resolve().as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if proc.returncode != 0 or not dst.exists():
            raise RuntimeError(
                f"chrome render failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace')[:500]}"
            )

    return _render
