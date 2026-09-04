"""Brønnøysund (Brreg) Regnskapsregisteret — open JSON company accounts (no API key)."""
from __future__ import annotations

import logging

from ._errors import note_error

log = logging.getLogger(__name__)

_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}"


def fetch_brreg_accounts(
    orgnr: str, *, fetcher, errors: "list[dict] | None" = None,
) -> list[dict]:
    """Every annual-accounts entry for an orgnr (a list of {regnskapsperiode,
    regnskapstype, valuta, resultatregnskapResultat, eiendeler, egenkapitalGjeld}).
    Returns [] on 404 / none / error — never raises. A caught exception is
    reported into ``errors`` when given (see :mod:`._errors`)."""
    try:
        data = fetcher.get_json(_URL.format(orgnr=orgnr))
    except Exception as exc:  # noqa: BLE001
        log.warning("Brreg accounts fetch failed for orgnr %s", orgnr, exc_info=True)
        note_error(errors, entity_id=orgnr, source="brreg",
                   stage="fetch_brreg_accounts", exc=exc)
        return []
    return data if isinstance(data, list) else []
