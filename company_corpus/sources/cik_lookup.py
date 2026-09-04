"""Name -> CIK index from the SEC ``cik-lookup-data.txt`` master file.

The SEC publishes a single file listing ``COMPANY NAME:CIK:`` for *every* filer,
**former names included**, so it covers delisted/renamed entities the current
ticker map omits. One GET, cached to disk thereafter. The file is undated (an
accumulation of every name ever borne); dates disambiguate collisions in a
second tier (see ``universe._tiebreak_collision_by_date``).
"""

from __future__ import annotations

from pathlib import Path

from ..config import normalize_cik
from ..http import Fetcher
from ..naming import canonical_name

CIK_LOOKUP_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"


def fetch_cik_lookup(fetcher: Fetcher, cache_path: Path | str) -> str:
    """Return the raw ``cik-lookup-data.txt`` text, reading the cache if present.

    On a cache miss, one GET via the shared (fair-access throttled) ``Fetcher``,
    then the body is persisted to ``cache_path``.
    """
    p = Path(cache_path)
    if p.exists():
        return p.read_text(encoding="utf-8")
    text = fetcher.get_text(CIK_LOOKUP_URL)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return text


def parse_cik_lookup(text: str) -> dict[str, set[str]]:
    """Parse ``NAME:CIK:`` lines into ``{canonical_name: {cik, ...}}``.

    A CIK with former names appears under several names (all -> same CIK); two
    distinct companies can share one canonical name (-> a set with >1 CIK = a
    collision). Malformed lines and empty names are skipped.
    """
    index: dict[str, set[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith(":"):
            line = line[:-1]
        name, sep, cik_raw = line.rpartition(":")
        if not sep or not name.strip():
            continue
        try:
            cik = normalize_cik(cik_raw)
        except ValueError:
            continue
        key = canonical_name(name)
        if not key:
            continue
        index.setdefault(key, set()).add(cik)
    return index
