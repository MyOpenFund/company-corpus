"""European issuer identity resolution (ticker / ISIN / LEI / name -> Entity).

Mirrors the US name->CIK resolver's multi-tier approach: try the most specific
identifier first, record the resolution tier as provenance, never guess an
ambiguous match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from ..gleif import fetch_gleif_record, parse_gleif_record
from ..openfigi import OPENFIGI_URL

GLEIF = "https://api.gleif.org/api/v1/lei-records"

# Name normalisation for the OpenFIGI->GLEIF bridge (below): GLEIF and OpenFIGI
# spell legal forms differently ("PLC" vs "Public Limited Company"), so collapse
# both to a comparable core before matching.
_NORM_DROP = re.compile(
    r"\b(?:plc|ltd|limited|sa|nv|se|ag|inc|oyj|asa|ab|group|holdings?|co|company|the)\b"
)
# Trailing legal form, stripped from the GLEIF *fulltext* query (GLEIF fulltext
# ANDs the tokens, and a trailing "PLC" matches no record -> 0 hits).
_LEGAL_TAIL = re.compile(
    r"[\s,]+(?:p\.?l\.?c\.?|ltd\.?|limited|s\.?a\.?|n\.?v\.?|se|ag|inc\.?|oyj|asa|ab)\.?$", re.I
)


def _norm_name(name: str) -> str:
    s = (name or "").lower().replace("public limited company", "plc")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = _NORM_DROP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _core_query(name: str) -> str:
    s = name or ""
    for _ in range(2):  # peel stacked forms, e.g. "… Holding AG"
        s = _LEGAL_TAIL.sub("", s).strip()
    return s

# Cap on ISINs fetched per entity. One ISIN is enough to key the ISIN-search OAM
# backends (BE/…); a few add robustness. The cap bounds both the GLEIF page and the
# downstream per-ISIN request fan-out.
_ISIN_CAP = 25


@dataclass(frozen=True)
class Entity:
    lei: str | None
    name: str
    country: str
    isins: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()
    resolution: str = ""  # "lei" | "isin" | "isin-figi" | "ticker" | "name" | "unresolved"


def _from_gleif_record(attrs: dict) -> tuple[str, str, str]:
    # (lei, legalName, country) — country falls back to the HQ address, then "".
    f = parse_gleif_record(attrs)
    return f["lei"], f["name"], f["legal_country"] or f["hq_country"] or ""


def _fetch_isins(lei: str, fetcher, *, seed: str = "", cap: int | None = None) -> tuple[str, ...]:
    """The issuer's ISINs from GLEIF (LEI->isins), capped. `seed` (the ISIN we resolved
    by, if any) is kept first. Degrades to () on any error — ISINs are best-effort."""
    cap = _ISIN_CAP if cap is None else cap  # read at call time so the cap stays configurable
    out: list[str] = [seed] if seed else []
    if lei:
        try:
            rows = fetcher.get_json(f"{GLEIF}/{quote(lei)}/isins?page%5Bsize%5D=100").get("data") or []
            for r in rows:
                isin = (r.get("attributes") or {}).get("isin")
                if isin and isin not in out:
                    out.append(isin)
                if len(out) >= cap:
                    break
        except Exception:
            pass  # ISINs are best-effort; a malformed/failed response yields what we have
    return tuple(out)


def _lookup_lei(lei: str, fetcher, *, with_isins: bool) -> Entity | None:
    record = fetch_gleif_record(lei, fetcher=fetcher)
    if not record:
        return None
    lei_v, name, country = _from_gleif_record(record["attributes"])
    isins = _fetch_isins(lei_v or lei, fetcher) if with_isins else ()
    return Entity(lei=lei_v or lei, name=name, country=country, isins=isins, resolution="lei")


def _openfigi_name(isin: str, fetcher) -> str | None:
    """Map an ISIN to its issuer name via OpenFIGI (broader ISIN coverage than
    GLEIF's ISIN->LEI mapping). Best-effort: ``None`` on any failure/miss."""
    try:
        res = fetcher.post_json(OPENFIGI_URL, [{"idType": "ID_ISIN", "idValue": isin}])
    except Exception:
        return None
    if isinstance(res, list) and res and isinstance(res[0], dict):
        data = res[0].get("data") or []
        if data:
            return data[0].get("name")
    return None


def _resolve_via_openfigi(isin: str, fetcher, *, with_isins: bool) -> Entity | None:
    """Fallback when GLEIF's ISIN->LEI mapping misses the ISIN: bridge through the
    issuer name (OpenFIGI) to a GLEIF LEI.

    No-guess: GLEIF is queried by the *core* name (legal form stripped, so its
    token-AND fulltext returns the issuer) and bound ONLY if exactly one record's
    normalised legal name equals the normalised OpenFIGI name.
    """
    name = _openfigi_name(isin, fetcher)
    want = _norm_name(name) if name else ""
    if not want:
        return None
    url = f"{GLEIF}?filter%5Bfulltext%5D={quote(_core_query(name))}&page%5Bsize%5D=50"
    try:
        rows = fetcher.get_json(url).get("data") or []
    except Exception:
        return None
    matches = [r for r in rows if _norm_name(_from_gleif_record(r["attributes"])[1]) == want]
    if len(matches) != 1:
        return None  # zero or ambiguous -> never bind
    lei_v, nm, ctry = _from_gleif_record(matches[0]["attributes"])
    isins = _fetch_isins(lei_v, fetcher, seed=isin) if with_isins else (isin,)
    return Entity(lei=lei_v, name=nm, country=ctry, isins=isins, resolution="isin-figi")


def _lookup_isin(isin: str, fetcher, *, with_isins: bool) -> Entity:
    url = f"{GLEIF}?filter%5Bisin%5D={quote(isin)}&page%5Bsize%5D=1"
    try:
        rows = fetcher.get_json(url).get("data") or []
    except Exception:
        rows = []
    if rows:
        lei_v, nm, ctry = _from_gleif_record(rows[0]["attributes"])
        isins = _fetch_isins(lei_v, fetcher, seed=isin) if with_isins else (isin,)
        return Entity(lei=lei_v, name=nm, country=ctry, isins=isins, resolution="isin")
    # GLEIF's ISIN->LEI mapping is incomplete (it can hold an issuer's LEI yet not
    # its equity ISIN); bridge through OpenFIGI before giving up.
    bridged = _resolve_via_openfigi(isin, fetcher, with_isins=with_isins)
    if bridged is not None:
        return bridged
    return Entity(lei=None, name="", country="", resolution="unresolved")


def _lookup_name(name: str, country: str, fetcher, *, with_isins: bool) -> Entity:
    url = f"{GLEIF}?filter%5Bentity.legalName%5D={quote(name)}&page%5Bsize%5D=10"
    try:
        rows = fetcher.get_json(url).get("data") or []
    except Exception:
        rows = []
    # If a country is given, keep only rows matching that country.
    if country:
        rows = [
            r for r in rows
            if _from_gleif_record(r["attributes"])[2] == country
        ]
    # Resolve ONLY if exactly one candidate remains — never guess an ambiguous match.
    if len(rows) == 1:
        lei_v, nm, ctry = _from_gleif_record(rows[0]["attributes"])
        isins = _fetch_isins(lei_v, fetcher) if with_isins else ()
        return Entity(lei=lei_v, name=nm, country=ctry, isins=isins, resolution="name")
    return Entity(lei=None, name=name, country=country, resolution="unresolved")


def resolve_entities(specs: list[dict], *, fetcher, populate_isins: bool = True) -> list[Entity]:
    """Resolve each spec to an Entity. When ``populate_isins`` (default), each resolved
    entity also carries the issuer's ISINs from GLEIF — the identity the ISIN-keyed OAM
    backends (e.g. Belgium) search on."""
    out: list[Entity] = []
    for spec in specs:
        if spec.get("lei"):
            e = _lookup_lei(spec["lei"], fetcher, with_isins=populate_isins)
            out.append(e or Entity(None, spec.get("name", ""), spec.get("country", ""),
                                    resolution="unresolved"))
        elif spec.get("isin"):
            out.append(_lookup_isin(spec["isin"], fetcher, with_isins=populate_isins))
        elif spec.get("name"):
            out.append(_lookup_name(spec["name"], spec.get("country", ""), fetcher,
                                    with_isins=populate_isins))
        else:  # ticker tier — requires OpenFIGI, deferred to Task 1c
            out.append(Entity(None, spec.get("name", ""), spec.get("country", ""),
                              resolution="unresolved"))
    return out
