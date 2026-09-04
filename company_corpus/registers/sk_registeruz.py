"""Slovak registeruz.sk — keyless JSON API client + positional extractor.

All public endpoints at ``https://www.registeruz.sk/cruz-public/api`` require
no API key or registration.  Data is structured-JSON with a *positional* flat
array per table, dimensioned by a per-template (*sablona*) column count.

Positional formula
------------------
For a given table index ``ti`` and row identified by ``cisloRiadku``:

    value[col] = data[row_arr_idx * ncols + col]

where:
* ``ncols``         = ``sablona["tabulky"][ti]["pocetDatovychStlpcov"]``  (from the
  *sablona* — the vykaz copy is present in committed fixtures but may differ or
  be absent in raw live responses; always read from the sablona).
* ``row_arr_idx``   = 0-based position of ``cisloRiadku`` in
  ``sablona["tabulky"][ti]["riadky"]``.
* ``col``           = 0-based column index (0 … ncols-1).

Template IDs
------------
699 (Úč POD) — standard Slovak accounting (assets table has 4 data columns;
liabilities + income statement tables have 2).
687 (Úč MUJ) — micro/individual entities (2 data columns per table).

Rate limiting
-------------
The entity-scan endpoint (``/uctovne-jednotky``) is WAF-throttled at scale.
Callers should add a politeness delay between pages (≥ 1 s).  The sablona
catalogue is tiny (< 20 distinct IDs); callers should cache fetched sablony.
Live / scale validation is a controller-level concern.
"""
from __future__ import annotations

import logging
from typing import Iterator

log = logging.getLogger(__name__)

BASE = "https://www.registeruz.sk/cruz-public/api"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _num(s: str) -> float | None:
    """Parse a numeric string from the registeruz data array.

    * Empty string  → ``None``.
    * Comma-decimal (``"1 051,30"``) → handled by stripping spaces then
      replacing ``,`` with ``"."``.
    * Plain integers (``"1051307"``) → ``float``.
    """
    if not isinstance(s, str) or s.strip() == "":
        return None
    cleaned = s.strip().replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Positional extractor
# ---------------------------------------------------------------------------

def parse_vykaz(vykaz: dict, sablona: dict) -> dict:
    """Extract all positional cell values from a vykaz using the sablona layout.

    Parameters
    ----------
    vykaz:
        Parsed JSON dict returned by ``/uctovny-vykaz?id=<id>``.
    sablona:
        Parsed JSON dict returned by ``/sablona?id=<idSablony>``.

    Returns
    -------
    dict with keys:

    ``idSablony`` : int
        Template identifier (e.g. 699 for Úč POD, 687 for Úč MUJ).
    ``pristupnostDat`` : str or None
        Accessibility flag — ``"Verejné"`` for public filings.
    ``ico`` : str or None
        Company registration number (IČO), 8 digits.
    ``cells`` : dict
        ``{(table_idx, cisloRiadku): [float | None, ...]}`` — one list per
        row, with ``ncols`` elements (``None`` for empty strings).  Empty when
        the vykaz carries no tables (IFRS / non-public).

    Notes
    -----
    ``ncols`` is read **exclusively** from the sablona
    (``sablona["tabulky"][ti]["pocetDatovychStlpcov"]``).  The raw vykaz
    response may or may not carry the same field; fixtures may inject it for
    convenience — the sablona is the authoritative source.
    """
    # Defensive .get() chains so malformed filings (e.g. idSablony=716 with
    # no titulnaStrana, 0 tables) return cells=={} without raising KeyError.
    id_sablony: int = vykaz.get("idSablony")
    pristupnost: str | None = vykaz.get("pristupnostDat")
    obsah: dict = vykaz.get("obsah") or {}
    ico: str | None = (obsah.get("titulnaStrana") or {}).get("ico")

    cells: dict[tuple[int, int], list[float | None]] = {}

    tabulky = obsah.get("tabulky", [])
    for ti, vt in enumerate(tabulky):
        st = sablona["tabulky"][ti]
        ncols: int = st["pocetDatovychStlpcov"]   # authoritative: from sablona
        data: list[str] = vt.get("data", [])
        for row_arr_idx, row_def in enumerate(st["riadky"]):
            cislo: int = row_def["cisloRiadku"]
            base_idx = row_arr_idx * ncols
            cells[(ti, cislo)] = [
                _num(data[base_idx + c]) if base_idx + c < len(data) else None
                for c in range(ncols)
            ]

    return {
        "idSablony": id_sablony,
        "pristupnostDat": pristupnost,
        "ico": ico,
        "cells": cells,
    }


# ---------------------------------------------------------------------------
# Keyless API client
# ---------------------------------------------------------------------------

def fetch_vykaz(id: int, *, fetcher) -> dict | None:
    """Fetch a single accounting statement (uctovny-vykaz) by numeric ID.

    Parameters
    ----------
    id:
        Numeric vykaz ID.
    fetcher:
        A :class:`~company_corpus.http.Fetcher` instance (or any object
        exposing ``get_json(url, *, params)``) .

    Returns
    -------
    dict or None
        Parsed JSON, or ``None`` on any error.  Batch-safe; never raises.
    """
    try:
        return fetcher.get_json(f"{BASE}/uctovny-vykaz", params={"id": id})
    except Exception:  # noqa: BLE001
        log.debug("SK fetch_vykaz failed for id=%s", id, exc_info=True)
        return None


def fetch_sablona(id: int, *, fetcher) -> dict | None:
    """Fetch a template (sablona) definition by numeric ID.

    Sablona IDs are few (< 20 distinct values).  Callers should cache the
    result and reuse it across multiple vykaz fetches.

    Parameters
    ----------
    id:
        Numeric sablona ID (e.g. 699, 687).
    fetcher:
        A :class:`~company_corpus.http.Fetcher` instance.

    Returns
    -------
    dict or None
        Parsed JSON, or ``None`` on any error.  Batch-safe; never raises.
    """
    try:
        return fetcher.get_json(f"{BASE}/sablona", params={"id": id})
    except Exception:  # noqa: BLE001
        log.debug("SK fetch_sablona failed for id=%s", id, exc_info=True)
        return None


def fetch_entity(id: int, *, fetcher) -> dict | None:
    """Fetch an accounting-entity (uctovna-jednotka) record by numeric ID.

    Parameters
    ----------
    id:
        Numeric entity ID.
    fetcher:
        A :class:`~company_corpus.http.Fetcher` instance.

    Returns
    -------
    dict or None
        Parsed JSON, or ``None`` on any error.  Batch-safe; never raises.
    """
    try:
        return fetcher.get_json(f"{BASE}/uctovna-jednotka", params={"id": id})
    except Exception:  # noqa: BLE001
        log.debug("SK fetch_entity failed for id=%s", id, exc_info=True)
        return None


def fetch_zavierka(id: int, *, fetcher) -> dict | None:
    """Fetch an annual-report closure (uctovna-zavierka) record by numeric ID.

    Parameters
    ----------
    id:
        Numeric zavierka ID.
    fetcher:
        A :class:`~company_corpus.http.Fetcher` instance.

    Returns
    -------
    dict or None
        Parsed JSON, or ``None`` on any error.  Batch-safe; never raises.
    """
    try:
        return fetcher.get_json(f"{BASE}/uctovna-zavierka", params={"id": id})
    except Exception:  # noqa: BLE001
        log.debug("SK fetch_zavierka failed for id=%s", id, exc_info=True)
        return None


def iter_entity_ids(
    *,
    fetcher,
    start: int = 0,
    max_zaznamov: int = 1000,
) -> Iterator[int]:
    """Iterate all accounting-entity IDs by paginating /uctovne-jednotky.

    The endpoint is WAF-throttled at scale — callers must add a politeness
    delay between pages (recommended: ≥ 1 s with ``time.sleep``).

    Parameters
    ----------
    fetcher:
        A :class:`~company_corpus.http.Fetcher` instance.
    start:
        First ``pokracovat-za-id`` value (default 0 → start from the
        beginning of the registry).
    max_zaznamov:
        Maximum IDs per page (default 1 000; API maximum is 1 000).

    Yields
    ------
    int
        Numeric entity IDs in ascending order.

    Notes
    -----
    Pagination stops when the API returns an empty ``"id"`` list or on any
    network error (batch-safe; never raises).
    """
    cursor = start
    while True:
        try:
            page = fetcher.get_json(
                f"{BASE}/uctovne-jednotky",
                params={"pokracovat-za-id": cursor, "max-zaznamov": max_zaznamov},
            )
        except Exception:  # noqa: BLE001
            log.debug(
                "SK iter_entity_ids failed at cursor=%s", cursor, exc_info=True
            )
            return

        ids: list[int] = page.get("id") or []
        if not ids:
            return

        yield from ids
        cursor = ids[-1]
