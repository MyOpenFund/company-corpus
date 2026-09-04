"""Keyless bulk acquisition for Companies House Accounts Bulk Data zips.

Iterates an accounts bulk zip (e.g. ``Accounts_Monthly_Data-2025-08.zip``)
and yields ``(ch_number, html_bytes)`` for every iXBRL ``.html`` file found.

Filename format inside the zip::

    Prod223_4212_<NUMBER>_<YYYYMMDD>.html

The third underscore-delimited field is the Companies House number; it is
normalised via :func:`~company_corpus.registers.identity._norm_ch_number`
(zero-pad to 8 if all-digits, uppercase, strip whitespace).

Inner ``.zip`` entries (CIC filings, ~0.04% of the bulk) are skipped; they
would need a second-level extraction pass that is out of scope here.
"""
from __future__ import annotations

import zipfile
from typing import Iterator

from company_corpus.registers.identity import _norm_ch_number


def iter_ch_bulk(
    zip_path: str,
    *,
    limit: int | None = None,
) -> Iterator[tuple[str, bytes]]:
    """Yield ``(ch_number, html_bytes)`` for each iXBRL account in *zip_path*.

    Parameters
    ----------
    zip_path:
        Path to a Companies House Accounts Bulk Data ``.zip`` file.
    limit:
        If given, stop after yielding *limit* items.
    """
    yielded = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            lower = name.lower()
            if lower.endswith(".zip"):
                # CIC filings are nested zips — skip (separate extraction path needed)
                continue
            if not lower.endswith(".html"):
                # Ignore index/metadata or any other non-HTML member
                continue

            # Extract NUMBER from  Prod223_4212_<NUMBER>_<YYYYMMDD>.html
            basename = name.rsplit("/", 1)[-1]  # strip any directory prefix
            parts = basename.split("_")
            if len(parts) < 4:
                continue
            ch_number = _norm_ch_number(parts[2])

            html_bytes = zf.read(name)
            yield ch_number, html_bytes

            yielded += 1
            if limit is not None and yielded >= limit:
                break
