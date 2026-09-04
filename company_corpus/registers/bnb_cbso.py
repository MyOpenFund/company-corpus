"""Belgium BNB CBSO Authentic Data API — annual-accounts deposit acquisition.

Fetches the latest annual-accounts deposit (XBRL / zip) for a Belgian company
from the BNB CBSO Authentic Data API (https://ws.cbso.nbb.be).

API key
-------
Registration is **free** at https://developer.cbso.nbb.be (self-service).
Live / scale validation — rate limits, pagination behaviour for entities with a
large deposit history, key-quota behaviour — is a **maintainer step** and is
intentionally out of scope for this module.

Public API
----------
fetch_bnb_deposit(kbo, *, fetcher, key, errors=None) -> bytes | None
    Returns the raw bytes of the latest deposit zip/xbrl, or None on any error
    (reported into ``errors`` when a list is given).
"""
from __future__ import annotations

import logging
import uuid

from ._errors import note_error

log = logging.getLogger(__name__)

BASE = "https://ws.cbso.nbb.be"


def _cbso_headers(key: str) -> dict:
    """Build per-request CBSO auth headers (each call gets a fresh Request-Id)."""
    return {
        "NBB-CBSO-Subscription-Key": key,
        "X-Request-Id": str(uuid.uuid4()),
    }


def _pick_latest(deposits: list[dict]) -> dict | None:
    """Return the deposit with the most recent DepositDate (ExerciseDates.EndDate
    as tie-breaker).  Returns None if the list is empty."""
    if not deposits:
        return None

    def _sort_key(d: dict):
        deposit_date = d.get("DepositDate") or ""
        exercise = d.get("ExerciseDates") or {}
        end_date = exercise.get("EndDate") or ""
        return (deposit_date, end_date)

    return max(deposits, key=_sort_key)


def fetch_bnb_deposit(
    kbo: str, *, fetcher, key: str, errors: "list[dict] | None" = None,
) -> bytes | None:
    """Fetch a Belgian company's latest annual-accounts deposit (XBRL).

    Parameters
    ----------
    kbo:
        KBO enterprise number (10-digit string, e.g. ``"0648822310"``).
    fetcher:
        A :class:`company_corpus.http.Fetcher` instance (or any object that
        exposes ``get_json(url, *, headers)`` and ``get(url, *, headers)``).
    key:
        CBSO Authentic Data API subscription key (free self-service registration
        at https://developer.cbso.nbb.be).
    errors:
        Optional list; a caught exception appends one record here (see
        :mod:`._errors`) so the caller can tell a dead API from "no deposits".

    Returns
    -------
    bytes or None
        Raw bytes of the latest deposit (a zip containing three XBRL files, or
        a bare ``.xbrl`` document depending on deposit model).  Returns ``None``
        on any error or if the entity has no deposits — **batch-safe, never raises**.

    Notes
    -----
    The returned bytes are intended to be consumed by
    ``registers.bnb_xbrl.open_bnb_deposit`` (zip) and
    ``registers.bnb_xbrl.parse_bnb_data_xbrl`` (xbrl).
    """
    refs_url = f"{BASE}/authentic/legalEntity/{kbo}/references"
    try:
        deposits = fetcher.get_json(refs_url, headers=_cbso_headers(key))
    except Exception as exc:  # noqa: BLE001
        log.warning("CBSO references fetch failed for KBO %s", kbo, exc_info=True)
        note_error(errors, entity_id=kbo, source="bnb", stage="fetch_bnb_deposit", exc=exc)
        return None

    if not deposits:
        return None

    latest = _pick_latest(deposits)
    if latest is None:
        return None

    # Prefer the URL embedded in the reference record; fall back to canonical pattern.
    # Use .get() so a missing ReferenceNumber produces a broken-but-harmless URL
    # that the inner try/except catches — never raises out of this function.
    acct_url: str = latest.get("AccountingDataURL") or (
        f"{BASE}/authentic/deposit/{latest.get('ReferenceNumber', '')}/accountingData"
    )
    try:
        resp = fetcher.get(
            acct_url,
            headers={**_cbso_headers(key), "Accept": "application/x.xbrl"},
        )
        return resp.content
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "CBSO accounting-data fetch failed for KBO %s ref %s",
            kbo,
            latest.get("ReferenceNumber"),
            exc_info=True,
        )
        note_error(errors, entity_id=kbo, source="bnb", stage="fetch_bnb_deposit", exc=exc)
        return None
