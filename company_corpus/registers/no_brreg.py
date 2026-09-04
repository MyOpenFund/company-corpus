"""Brønnøysund (Brreg) Regnskapsregisteret — open JSON company accounts (no API key)."""
from __future__ import annotations

_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}"


def fetch_brreg_accounts(orgnr: str, *, fetcher) -> list[dict]:
    """Every annual-accounts entry for an orgnr (a list of {regnskapsperiode,
    regnskapstype, valuta, resultatregnskapResultat, eiendeler, egenkapitalGjeld}).
    Returns [] on 404 / none / error — never raises."""
    try:
        data = fetcher.get_json(_URL.format(orgnr=orgnr))
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else []
