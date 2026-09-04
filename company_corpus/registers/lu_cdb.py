"""Luxembourg Centrale-des-bilans (LBR) bulk-XML acquisition — keyless.

Downloads quarterly bulk XML files from data.public.lu (CC-BY-SA, no API key
required) and iterates the ``<Declarer>`` elements they contain.

Acquisition model
-----------------
The LBR publishes bulk STATEC/eCDF XML dumps at::

    https://download.data.public.lu/resources/donnees-comptes-annuels/...

Each file covers one quarter and may contain tens of thousands of declarers.
Access is completely open (CC-BY-SA); no registration or key is needed.

Public API
----------
iter_lu_declarers(xml_source, *, rcs_filter=None)
    Parse a bulk XML (path or bytes) and yield one dict per ``<Declarer>``.
    Optionally filter to a set of RCS strings.

download_lu_quarter(url, *, fetcher) -> bytes
    Keyless GET of a quarterly bulk XML.  Returns the raw bytes.
    Raises a clear ``RuntimeError`` on HTTP/network failure — callers that need
    batch-safe skip behaviour should catch ``Exception``.
"""
from __future__ import annotations

import logging
import os
from typing import Iterator, Union

from company_corpus.registers.lu_ecdf import parse_lu_declarers

log = logging.getLogger(__name__)


def iter_lu_declarers(
    xml_source: Union[str, bytes, os.PathLike],
    *,
    rcs_filter: set[str] | None = None,
) -> Iterator[dict]:
    """Iterate declarers from a Luxembourg Centrale-des-bilans bulk XML.

    Parameters
    ----------
    xml_source:
        File path (str or Path) **or** raw bytes of a STATEC/eCDF bulk XML
        document as published on data.public.lu.
    rcs_filter:
        Optional set of RCS strings (e.g. ``{"B60814", "B138357"}``).  When
        provided, only declarers whose ``rcs`` is in the set are yielded.
        Pass ``None`` (the default) to yield every declarer in the file.

    Yields
    ------
    dict
        One dict per ``<Declarer>`` with the shape produced by
        :func:`company_corpus.registers.lu_ecdf.parse_lu_declarers`::

            {
                "rcs":  str,
                "name": str,
                "declarations": [
                    {
                        "type":       str,
                        "model":      str,
                        "currency":   str,
                        "period_end": str | None,
                        "fields":     {int: float},
                    }
                ]
            }

    Notes
    -----
    Data source: data.public.lu bulk XML, CC-BY-SA, keyless.
    Acquisition strategy: bulk-scan (one file per quarter, no per-entity API).
    """
    for declarer in parse_lu_declarers(xml_source):
        if rcs_filter is None or declarer["rcs"] in rcs_filter:
            yield declarer


def download_lu_quarter(url: str, *, fetcher) -> bytes:
    """Download a quarterly Centrale-des-bilans bulk XML from data.public.lu.

    No API key or authentication is required; the files are published under
    CC-BY-SA at ``https://download.data.public.lu/resources/donnees-comptes-annuels/``.

    Parameters
    ----------
    url:
        Full URL of the quarterly bulk XML, e.g.::

            https://download.data.public.lu/resources/donnees-comptes-annuels/
            20240101T000000/comptes-annuels-2023-Q4.xml

    fetcher:
        A :class:`company_corpus.http.Fetcher` instance (or any object that
        exposes ``get(url) -> response`` with a ``.content`` attribute).

    Returns
    -------
    bytes
        Raw bytes of the bulk XML.

    Raises
    ------
    RuntimeError
        If the download fails (HTTP error, network failure, etc.).  The original
        exception is chained.  Callers that need batch-safe skip behaviour should
        wrap the call in ``try/except Exception``.

    Notes
    -----
    Data source: data.public.lu, CC-BY-SA, keyless (no registration required).
    Acquisition model: bulk-scan — one quarterly file covers all LU declarers
    for that period; no per-entity API call is made.
    """
    try:
        resp = fetcher.get(url)
        return resp.content
    except Exception as exc:
        raise RuntimeError(f"Failed to download LU quarter from {url}: {exc}") from exc
