"""Finnish PRH XBRL open API — keyless acquisition (avoindata.prh.fi).

Provides three public functions for acquiring XBRL financial data from the
Finnish Patent and Registration Office (PRH) open-data API:

* :func:`fetch_fi_financial` — fetch a single XBRL instance document by
  business-ID + financial date.
* :func:`list_fi_dates` — list available ``financialDate`` values for a business.
* :func:`iter_fi_all` — iterate all companies that have filed for a given date
  (paginated).

API key
-------
No key or authentication required.  The PRH open-data API is fully public at
``https://avoindata.prh.fi`` — no registration, no headers.

Live / scale validation — rate limits, pagination edge cases, completeness of
the filing universe for a given date — is a **controller step** and is
intentionally out of scope for this module.
"""
from __future__ import annotations

import logging
from typing import Iterator

log = logging.getLogger(__name__)

BASE = "https://avoindata.prh.fi/opendata-xbrl-api/v3"

_PAGE_SIZE = 100  # items returned per page by the API


def fetch_fi_financial(
    business_id: str,
    financial_date: str,
    *,
    fetcher,
) -> bytes | None:
    """Fetch the XBRL instance document for one company and period.

    Parameters
    ----------
    business_id:
        Finnish Y-tunnus, e.g. ``"2919415-2"``.
    financial_date:
        ISO date string for the period end, e.g. ``"2024-12-31"``.
    fetcher:
        A :class:`company_corpus.http.Fetcher` instance (or any object that
        exposes ``get(url, *, params) -> response`` where ``response.content``
        is the raw bytes).

    Returns
    -------
    bytes or None
        Raw XBRL/XML bytes, or ``None`` on any error.  **Batch-safe; never
        raises.**

    Notes
    -----
    No ``Accept`` header is sent.  The PRH API returns XML by default and
    returns HTTP 400 when ``Accept: application/xml`` is present.
    """
    url = f"{BASE}/financial"
    params = {"businessId": business_id, "financialDate": financial_date}
    try:
        resp = fetcher.get(url, params=params)
        return resp.content
    except Exception:  # noqa: BLE001
        log.debug(
            "PRH fetch_fi_financial failed for %s / %s",
            business_id,
            financial_date,
            exc_info=True,
        )
        return None


def list_fi_dates(business_id: str, *, fetcher) -> list[str]:
    """List available ``financialDate`` strings for a business.

    Parameters
    ----------
    business_id:
        Finnish Y-tunnus, e.g. ``"2919415-2"``.
    fetcher:
        A :class:`company_corpus.http.Fetcher` instance exposing
        ``get_json(url, *, params)``.

    Returns
    -------
    list[str]
        List of ISO date strings (e.g. ``["2022-12-31", "2023-12-31"]``) as
        returned by the API.  Returns ``[]`` on any error — **batch-safe,
        never raises**.
    """
    url = f"{BASE}/financials"
    try:
        data = fetcher.get_json(url, params={"businessId": business_id})
        # Real API: {"totalResults": N, "financials": [{"businessId": …, "financialDate": …}, …]}
        if isinstance(data, dict):
            items = data.get("financials") or []
            return [
                it["financialDate"]
                for it in items
                if isinstance(it, dict) and it.get("financialDate")
            ]
        # Defensive fallback for any hypothetical bare-list response
        if isinstance(data, list):
            return [it for it in data if isinstance(it, str)]
        return []
    except Exception:  # noqa: BLE001
        log.debug("PRH list_fi_dates failed for %s", business_id, exc_info=True)
        return []


def iter_fi_all(financial_date: str, *, fetcher) -> Iterator[str]:
    """Iterate all companies that have filed for *financial_date*.

    Paginates via the ``page`` query parameter (100 items per page as returned
    by the API).  Stops when a page contains fewer than 100 items (last page)
    or is empty.

    Parameters
    ----------
    financial_date:
        ISO date string, e.g. ``"2024-12-31"``.
    fetcher:
        A :class:`company_corpus.http.Fetcher` instance exposing
        ``get_json(url, *, params)``.

    Yields
    ------
    str
        ``businessId`` (Y-tunnus) for each company in the filing set.

    Notes
    -----
    Batch-safe: any error on a page silently stops iteration so the caller can
    process all items received before the failure without losing partial data.
    """
    url = f"{BASE}/all_financials"
    page = 1
    while True:
        try:
            resp = fetcher.get_json(
                url, params={"financialDate": financial_date, "page": page}
            )
        except Exception:  # noqa: BLE001
            log.debug(
                "PRH iter_fi_all failed on page %d for date %s",
                page,
                financial_date,
                exc_info=True,
            )
            return

        # Real API response: {"totalResults": N, "financials": [{businessId, …}, …]}
        if not isinstance(resp, dict):
            log.debug("PRH iter_fi_all unexpected response type %r on page %d", type(resp), page)
            return

        items = resp.get("financials") or []
        if not items:
            return

        for item in items:
            bid = item.get("businessId")
            if bid:
                yield bid

        if len(items) < _PAGE_SIZE:
            return  # last page reached

        page += 1
