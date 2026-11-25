"""Company-resolution rows without a store (live, no-vault mode).

Rows are shaped like the ``entity_observations`` rows myopenfund-mcp's ``resolve.index``
reads: ``subject_value`` (10-digit CIK), ``name``, ``id_scheme``, ``id_value``,
``valid_date``, ``entity_id``, ``country``.
"""
from __future__ import annotations

import json
from datetime import date

from ..opendata.identity import _day
from ..opendata.submissions import SubmissionsArchive
from .sec import SecGet

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def _row(cik: str, name: str | None, *, scheme: str | None = None, value: str | None = None,
         valid_date: date | None = None) -> dict:
    return {"subject_value": cik, "name": name, "id_scheme": scheme, "id_value": value,
            "valid_date": valid_date, "entity_id": None, "country": None}


def _cik(raw) -> str | None:
    text = str(raw).strip() if isinstance(raw, (int, str)) and not isinstance(raw, bool) else ""
    return text.zfill(10) if text.isdigit() and len(text) <= 10 else None


def resolver_rows(sec: SecGet, *, max_age_s: float | None) -> list[dict]:
    """One ``ticker`` row per entry of the SEC tickers file (mutable: the caller sets the TTL),
    in file order. Entries without a usable CIK or ticker are skipped."""
    body = json.loads(sec.get_bytes(TICKERS_URL, max_age_s=max_age_s))
    if not isinstance(body, dict):
        raise ValueError("company_tickers.json: expected a JSON object")
    rows = []
    for entry in body.values():
        if not isinstance(entry, dict):
            continue
        cik = _cik(entry.get("cik_str"))
        ticker = entry.get("ticker").strip() if isinstance(entry.get("ticker"), str) else ""
        if cik is None or not ticker:
            continue
        title = entry.get("title").strip() if isinstance(entry.get("title"), str) else ""
        rows.append(_row(cik, title or None, scheme="ticker", value=ticker))
    return rows


def former_name_rows(archive: SubmissionsArchive) -> list[dict]:
    """Name-only rows (no ``id_scheme``) from the submissions ``formerNames``, each dated by its
    ``from``, then the current name dated by the latest former ``to`` (the archive's fetch day
    when there is none), as the store producer dates them, so ``resolve.index`` ends on the
    current name. Undated or blank former names are skipped."""
    main, cik = archive.main, archive.cik
    formers = []
    for f in main.get("formerNames") or []:
        if not isinstance(f, dict):
            continue
        name, start = (f.get("name") or "").strip(), _day(f.get("from"))
        if name and start is not None:
            formers.append((start, _day(f.get("to")), name))
    formers.sort(key=lambda x: (x[0], x[1] or date.max, x[2]))
    rows = [_row(cik, name, valid_date=start) for start, _, name in formers]
    ends = [end for _, end, _ in formers if end is not None]
    current = (main.get("name") or "").strip() if isinstance(main.get("name"), str) else ""
    if current:
        rows.append(_row(cik, current, valid_date=max(ends) if ends else archive.fetched_at.date()))
    return rows
