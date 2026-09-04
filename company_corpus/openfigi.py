"""OpenFIGI identifier enrichment (isolated, optional, jurisdiction-general).

Maps a security identifier (ISIN/CUSIP) to its OpenFIGI record -- issuer name,
ticker, security type, exchange, FIGI. OpenFIGI returns no CIK, so this is an
*enrichment / triage* aid, not a resolver: it identifies who an issuer is and
classifies whether the security is publicly registered (a candidate for its
jurisdiction's filings registry) or a private placement (reachable nowhere).

Isolation: imported by nothing in the core pipeline, depends only on the standard
library (``urllib``), and the HTTP POST is injectable (``post=``) so callers/tests
can supply their own transport.

API: https://www.openfigi.com/api -- free; an optional key raises rate limits
(without a key: 5 jobs/request, 25 requests/minute).
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
_ID_TYPES = {"isin": "ID_ISIN", "cusip": "ID_CUSIP", "ID_ISIN": "ID_ISIN", "ID_CUSIP": "ID_CUSIP"}


@dataclass(frozen=True)
class FigiRecord:
    """One OpenFIGI match for an identifier (first/best hit)."""

    name: str = ""
    ticker: str = ""
    security_type: str = ""
    market_sector: str = ""
    exch_code: str = ""
    figi: str = ""


def coverage_hint(security_type: str) -> str:
    """Jurisdiction-neutral triage from OpenFIGI ``securityType``.

    * ``registry_candidate`` -- a publicly registered security (``GLOBAL`` /
      ``*DOMESTIC*``); a candidate for its jurisdiction's filings registry (EDGAR
      for the US; EDINET/DART/... elsewhere). Does **not** assert presence in any
      specific registry.
    * ``private_placement`` -- a 144A/Reg-S private placement (``PRIV`` / ``144A`` /
      ``REG-S``); in no public registry, anywhere.
    * ``unknown`` -- ``securityType`` absent or unrecognized.

    Private-placement markers are checked **first**, so a compound type like
    ``"GLOBAL 144A"`` classifies as ``private_placement`` (a 144A note is *not*
    publicly registered, regardless of the ``GLOBAL`` token).
    """
    s = (security_type or "").upper()
    if "PRIV" in s or "144A" in s or "REG-S" in s:
        return "private_placement"
    if "DOMESTIC" in s or "GLOBAL" in s:
        return "registry_candidate"
    return "unknown"


def _default_post(url: str, body: bytes, headers: dict) -> list:
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https endpoint
        return json.loads(resp.read())


def map_identifiers(
    values: Iterable[str],
    *,
    id_type: str = "isin",
    api_key: str | None = None,
    post: Callable[[str, bytes, dict], list] | None = None,
    batch_size: int | None = None,
    pause: float = 1.0,
) -> dict[str, FigiRecord | None]:
    """Map identifiers to :class:`FigiRecord`s (``None`` when OpenFIGI has no hit).

    ``post`` is the injectable transport ``(url, body, headers) -> list`` aligned
    with the batch; defaults to a stdlib ``urllib`` POST. ``pause`` seconds are
    slept between batches to respect rate limits (set ``0`` in tests).
    """
    figi_id_type = _ID_TYPES.get(id_type, id_type)
    post = post or _default_post
    if batch_size is None:
        batch_size = 100 if api_key else 5
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key

    vals = list(values)
    out: dict[str, FigiRecord | None] = {}
    for start in range(0, len(vals), batch_size):
        batch = vals[start:start + batch_size]
        body = json.dumps([{"idType": figi_id_type, "idValue": v} for v in batch]).encode()
        results = post(OPENFIGI_URL, body, headers)
        for i, value in enumerate(batch):
            # Index by position so every input gets a key even if OpenFIGI returns
            # a short/truncated result list (missing -> None).
            result = results[i] if i < len(results) else None
            data = (result or {}).get("data") if isinstance(result, dict) else None
            if data:
                d = data[0]
                out[value] = FigiRecord(
                    name=d.get("name", ""), ticker=d.get("ticker", ""),
                    security_type=d.get("securityType", ""),
                    market_sector=d.get("marketSector", ""),
                    exch_code=d.get("exchCode", ""), figi=d.get("figi", ""),
                )
            else:
                out[value] = None
        if pause and start + batch_size < len(vals):
            time.sleep(pause)
    return out
