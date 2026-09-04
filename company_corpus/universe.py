"""Issuer universe registry.

Parallels ``cb_corpus.banks``: a version-controlled list of the entities we
crawl. cb_corpus targets 63 named central banks; here the curated tier is a list
of company issuers identified by CIK (with ticker/name for humans).

Ticker -> CIK resolution uses the SEC's official map
``https://www.sec.gov/files/company_tickers.json``. Curated lists are stored as
JSONL under ``data/universe/<name>.jsonl`` and committed to the repo.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .config import Config, cusip6 as to_cusip6, cusip_full as to_cusip_full, normalize_cik
from .http import Fetcher
from .naming import canonical_name, name_as_of, parse_former_names

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


@dataclass(frozen=True)
class Issuer:
    """One company in the universe.

    ``cik`` may be empty when it could not be resolved (e.g. a since-delisted
    index member). ``first_seen``/``last_seen`` are optional index-membership
    dates (ISO, or ``"current"`` for present members).
    """

    cik: str
    ticker: str = ""
    company: str = ""
    first_seen: str = ""
    last_seen: str = ""
    cusip6: str = ""
    resolution: str = ""  # how the CIK was resolved: "cik", "ticker", "cusip", or "both"

    def __post_init__(self) -> None:
        object.__setattr__(self, "cik", normalize_cik(self.cik) if self.cik else "")


def load_company_tickers(fetcher: Fetcher) -> dict[str, Issuer]:
    """Fetch the SEC ticker map; return ``{TICKER: Issuer}`` (upper-cased).

    When a ticker maps to more than one CIK (rare, but possible across the SEC
    feed), the lowest CIK wins -- a deterministic tie-break independent of the
    feed's row order -- and a warning is emitted so the collision is visible
    instead of silently resolving to whichever row happened to come last.
    """
    data = fetcher.get_json(COMPANY_TICKERS_URL)
    # The map is keyed by arbitrary index strings: {"0": {cik_str, ticker, title}, ...}
    rows = data.values() if isinstance(data, dict) else data
    out: dict[str, Issuer] = {}
    collisions: set[str] = set()
    for row in rows:
        ticker = str(row.get("ticker", "")).upper()
        if not ticker:
            continue
        issuer = Issuer(
            cik=normalize_cik(row["cik_str"]),
            ticker=ticker,
            company=row.get("title", ""),
        )
        existing = out.get(ticker)
        if existing is None:
            out[ticker] = issuer
        elif existing.cik != issuer.cik:
            collisions.add(ticker)
            if issuer.cik < existing.cik:  # deterministic: lowest CIK wins
                out[ticker] = issuer
    if collisions:
        sample = ", ".join(sorted(collisions)[:10])
        warnings.warn(
            f"company_tickers.json: {len(collisions)} ticker(s) map to multiple "
            f"CIKs; kept the lowest CIK for each ({sample})",
            stacklevel=2,
        )
    return out


def resolve_tickers(
    tickers: Iterable[str], fetcher: Fetcher
) -> tuple[list[Issuer], list[str]]:
    """Resolve tickers to :class:`Issuer`s. Returns ``(issuers, unresolved)``.

    NOTE: ``company_tickers.json`` lists *currently* trading issuers only, so a
    ticker-built universe has **survivorship bias** — delisted, acquired, or
    failed companies (Lehman, Twitter/TWTR, Enron, …) won't resolve. For
    historical coverage, anchor on CIK via :func:`resolve_ciks` or crawl the
    full-index (see ``sources.edgar_index``).
    """
    table = load_company_tickers(fetcher)
    issuers: list[Issuer] = []
    unresolved: list[str] = []
    for raw in tickers:
        t = raw.strip().upper()
        if not t:
            continue
        issuer = table.get(t)
        if issuer:
            issuers.append(issuer)
        else:
            unresolved.append(t)
    return issuers, unresolved


_NAME_NOISE = {
    "INC", "CORP", "CORPORATION", "CO", "COMPANY", "COS", "LLC", "LP", "PLC", "SA",
    "NV", "AG", "AB", "LTD", "LIMITED", "GROUP", "HOLDINGS", "HOLDING", "FINANCE",
    "FINANCIAL", "CAPITAL", "FUNDING", "USA", "US", "INTERNATIONAL", "INTL", "THE",
    "OF", "AND", "&",
}


def _name_tokens(name: str) -> set[str]:
    """Significant alphanumeric tokens of a company name (legal-form words dropped)."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in str(name).upper())
    return {t for t in cleaned.split() if t and t not in _NAME_NOISE}


def _names_match(a: str, b: str) -> bool:
    """True when two company names share a significant token (ignoring legal forms).

    Deliberately loose: one shared token counts (so "Coca-Cola Company" ~ "COCA
    COLA CO"), which can over-match ("American Airlines" ~ "American Express").
    Acceptable: a match only downgrades a collision's review priority, never drops.
    """
    ta, tb = _name_tokens(a), _name_tokens(b)
    return bool(ta and tb and (ta & tb))


def _find_column(headers: list[str], candidates: Iterable[str]) -> str | None:
    """Return the first header whose lower-cased name matches a candidate."""
    lower = {h.lower().strip(): h for h in headers}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    return None


def read_identifier_csv(
    path: Path | str, *, ticker_col: str | None = None,
    cusip_col: str | None = None, cik_col: str | None = None,
) -> list[dict]:
    """Read a CSV of issuer identifiers into one row per issuer.

    Auto-detects a CIK column (``CIK``), a ticker column (``Ticker``), and a
    CUSIP/ISIN column (``CUSIP`` preferred, else ``ISIN``) unless overridden; a
    name column (``Issuer``/``Company``/``Name``) is used for humans. Rows are
    grouped by ticker if present, else by CIK, else by CUSIP6; within a group the
    most common CIK and CUSIP6 are kept (a bond file has many rows per issuer).
    Returns ``[{cik, ticker, cusip6, name}, ...]``.
    """
    import csv
    from collections import Counter

    with Path(path).open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        kcol = cik_col or _find_column(headers, ("cik",))
        tcol = ticker_col or _find_column(headers, ("ticker",))
        ccol = cusip_col or _find_column(headers, ("cusip", "isin"))
        ncol = _find_column(headers, ("issuer", "company", "name"))
        if not (kcol or tcol or ccol):
            raise ValueError(
                f"CSV needs a CIK, ticker, or CUSIP/ISIN column; headers: {headers}")
        records = list(reader)

    groups: dict[str, dict] = {}
    for row in records:
        cik = (row.get(kcol, "") if kcol else "").strip()
        ticker = (row.get(tcol, "") if tcol else "").strip().upper()
        raw_id = (row.get(ccol, "") if ccol else "").strip()
        c6 = to_cusip6(raw_id) if raw_id else ""
        full = to_cusip_full(raw_id) if raw_id else ""
        name = (row.get(ncol, "") if ncol else "").strip()
        key = ticker or cik or c6
        if not key:
            continue
        g = groups.setdefault(key, {"ticker": ticker, "cik_votes": Counter(),
                                    "cusip6_votes": Counter(), "cusip_votes": Counter(),
                                    "name": ""})
        if cik:
            g["cik_votes"][cik] += 1
        if c6:
            g["cusip6_votes"][c6] += 1
        if full:
            g["cusip_votes"][full] += 1
        if name and not g["name"]:
            g["name"] = name

    out: list[dict] = []
    for g in groups.values():
        cik_votes: Counter = g["cik_votes"]
        c6_votes: Counter = g["cusip6_votes"]
        full_votes: Counter = g["cusip_votes"]
        out.append({
            "cik": cik_votes.most_common(1)[0][0] if cik_votes else "",
            "ticker": g["ticker"],
            "cusip6": c6_votes.most_common(1)[0][0] if c6_votes else "",
            "cusip": full_votes.most_common(1)[0][0] if full_votes else "",
            "name": g["name"],
        })
    return out


def reconcile_identifiers(
    rows: Iterable[dict],
    ticker_table: dict[str, Issuer],
    crosswalk: dict[str, set[str]],
    *,
    fts=None,
    fts_limit: int | None = None,
    name_index: dict[str, set[str]] | None = None,
    name_cache: dict[str, str] | None = None,
) -> tuple[list[Issuer], list[dict], list[str]]:
    """Resolve each row by authority CIK > CUSIP > ticker, cross-checking ticker vs CUSIP.

    Returns ``(issuers, collisions, unresolved)``:

    * a valid **provided CIK** wins (``resolution="cik"``); ticker/cusip on that row
      are not used (v1 trusts the explicit CIK without cross-checking it);
    * else **both** ticker->CIK and CUSIP6->CIK agree -> ``resolution="both"``; only
      one resolves -> ``"ticker"`` / ``"cusip"``;
    * else **both resolve but disagree** -> a ``collision`` dict (recycled-ticker
      hazard), classified ``name_match`` / ``name_mismatch`` and **excluded** from
      ``issuers`` (caller decides what to do);
    * else -> the identifier string in ``unresolved``.
    """
    rows = list(rows)
    all_c6 = [(r.get("cusip6") or "").strip().upper() for r in rows]
    cusip_ciks, _ = resolve_cusips([c for c in all_c6 if c], crosswalk)

    issuers: list[Issuer] = []
    collisions: list[dict] = []
    unresolved: list[str] = []
    fts_calls = 0
    for row in rows:
        raw_cik = (row.get("cik") or "").strip()
        ticker = (row.get("ticker") or "").strip().upper()
        c6 = (row.get("cusip6") or "").strip().upper()
        name = (row.get("name") or "").strip()

        if raw_cik:
            try:
                cik = normalize_cik(raw_cik)
            except ValueError:
                cik = ""
            if cik:
                issuers.append(Issuer(cik=cik, ticker=ticker, company=name,
                                      cusip6=c6, resolution="cik"))
                continue

        from_ticker = ticker_table.get(ticker) if ticker else None
        cik_ticker = from_ticker.cik if from_ticker else ""
        cik_cusip = cusip_ciks.get(c6, "") if c6 else ""
        company = (from_ticker.company if from_ticker else "") or name

        if cik_ticker and cik_cusip:
            if cik_ticker == cik_cusip:
                issuers.append(Issuer(cik=cik_ticker, ticker=ticker, company=company,
                                      cusip6=c6, resolution="both"))
            else:
                sec_ticker_name = from_ticker.company if from_ticker else ""
                kind = "name_match" if _names_match(name, sec_ticker_name) else "name_mismatch"
                collisions.append({"ticker": ticker, "cusip6": c6, "name": name,
                                   "cik_ticker": cik_ticker, "cik_cusip": cik_cusip,
                                   "sec_ticker_name": sec_ticker_name, "kind": kind})
        elif cik_ticker:
            issuers.append(Issuer(cik=cik_ticker, ticker=ticker, company=company,
                                  cusip6=c6, resolution="ticker"))
        elif cik_cusip:
            issuers.append(Issuer(cik=cik_cusip, ticker=ticker, company=company,
                                  cusip6=c6, resolution="cusip"))
        else:
            # Name tier (local/cached) before fts (per-row network).
            name_cik = ""
            if name_index is not None and name:
                status, val = _resolve_one_name(name, name_index, name_cache or {})
                if status == "resolved":
                    name_cik = val  # type: ignore[assignment]
            if name_cik:
                issuers.append(Issuer(cik=name_cik, ticker=ticker, company=company,
                                      cusip6=c6, resolution="name"))
                continue
            full_cusip = (row.get("cusip") or "").strip().upper()
            hit = None
            if fts is not None and full_cusip and (fts_limit is None or fts_calls < fts_limit):
                fts_calls += 1
                hit = fts.resolve(full_cusip)
            if hit:
                hit_cik, hit_name = hit
                kind = "confirmed" if _names_match(name, hit_name) else "unverified"
                issuers.append(Issuer(cik=hit_cik, ticker=ticker, company=name,
                                      cusip6=c6, resolution=f"fts:{kind}"))
            else:
                unresolved.append(name or ticker or c6)
    return issuers, collisions, unresolved


def load_cusip_crosswalk(path: Path | str) -> dict[str, set[str]]:
    """Load a CUSIP6 -> {CIK} crosswalk CSV into ``{cusip6_upper: {cik, ...}}``.

    Expects a ``cik,cusip6,cusip8`` schema. The common public feed serializes the
    CIK as a float string (e.g. ``"320193.0"``), so we drop any fractional part
    before normalizing -- otherwise ``normalize_cik`` would fold the trailing
    ``.0`` into a spurious digit. Offline: no network.
    """
    import csv

    out: dict[str, set[str]] = {}
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            raw6 = (row.get("cusip6") or "").strip().upper()
            raw_cik = (row.get("cik") or "").split(".")[0]
            if not raw6 or not raw_cik.strip():
                continue
            try:
                cik = normalize_cik(raw_cik)
            except ValueError:
                continue
            out.setdefault(raw6, set()).add(cik)
    return out


def write_cusip_crosswalk(path: Path | str, pairs: Iterable[tuple[str, str]]) -> int:
    """Merge ``(cik, cusip6)`` pairs into a ``cik,cusip6`` CSV at ``path`` (dedup).

    Loads any existing rows (via the same schema :func:`load_cusip_crosswalk`
    reads), unions the new pairs (CIKs normalized to 10 digits, CUSIP6 upper-cased),
    and rewrites a sorted, de-duplicated CSV. Returns the total row count. Pure file
    I/O -- used to grow the ``--fts-cache`` across runs.
    """
    import csv

    p = Path(path)
    rows: set[tuple[str, str]] = set()
    if p.exists():
        for c6, ciks in load_cusip_crosswalk(p).items():
            for cik in ciks:
                rows.add((cik, c6))
    for cik, c6 in pairs:
        cik = str(cik).strip()
        c6 = str(c6).strip().upper()
        if not cik or not c6:
            continue
        try:
            rows.add((normalize_cik(cik), c6))
        except ValueError:
            continue

    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["cik", "cusip6"])
        for cik, c6 in sorted(rows):
            w.writerow([cik, c6])
    return len(rows)


def resolve_cusips(
    cusip6s: Iterable[str], crosswalk: dict[str, set[str]]
) -> tuple[dict[str, str], list[str]]:
    """Resolve CUSIP6 prefixes to CIKs via ``crosswalk``. Returns ``(resolved, unresolved)``.

    Only an unambiguous (single-CIK) match resolves; a CUSIP6 absent **or** mapping
    to several CIKs goes to ``unresolved``. A warning surfaces the ambiguous count.
    """
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    ambiguous: list[str] = []
    for raw in cusip6s:
        key = str(raw).strip().upper()
        if not key:
            continue
        ciks = crosswalk.get(key)
        if ciks and len(ciks) == 1:
            resolved[key] = next(iter(ciks))
        else:
            unresolved.append(key)
            if ciks:
                ambiguous.append(key)
    if ambiguous:
        warnings.warn(
            f"cusip crosswalk: {len(ambiguous)} CUSIP6(s) map to multiple CIKs; "
            f"left unresolved ({', '.join(sorted(ambiguous)[:10])})",
            stacklevel=2,
        )
    return resolved, unresolved


def _resolve_one_name(
    name: str, index: dict[str, set[str]], cache: dict[str, str]
) -> tuple[str, object]:
    """Resolve a single name against the cache then the index (no date tier).

    Returns ``("resolved", cik)``, ``("collision", [cik, ...])`` (sorted), or
    ``("unresolved", None)``. The cache (keyed by canonical name) wins and also
    short-circuits collision flagging.
    """
    key = canonical_name(name)
    if not key:
        return ("unresolved", None)
    if key in cache:
        return ("resolved", cache[key])
    ciks = index.get(key)
    if not ciks:
        return ("unresolved", None)
    if len(ciks) == 1:
        return ("resolved", next(iter(ciks)))
    return ("collision", sorted(ciks))


def _coerce_date(value) -> date | None:
    """Best-effort ISO date from a string/``date``; ``None`` for ``""``/``"current"``."""
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _tiebreak_collision_by_date(
    key: str, candidates: list[str], target: date, fetcher: Fetcher
) -> str:
    """Return the single candidate CIK whose name-in-effect on ``target``
    canonicalizes to ``key``; ``""`` if zero or more than one qualify.

    Reads each candidate's dated ``formerNames`` via the submissions API and
    applies :func:`naming.name_as_of`. One submissions GET per candidate -- only
    reached for an already-ambiguous name with a known date.
    """
    from .sources.edgar_submissions import SUBMISSIONS_URL

    survivors: list[str] = []
    for cik in candidates:
        try:
            data = fetcher.get_json(SUBMISSIONS_URL.format(cik=cik))
        except Exception:  # noqa: BLE001 - a failed lookup just can't vouch for this CIK
            continue
        eff = name_as_of(target, data.get("name", ""),
                         parse_former_names(data.get("formerNames")))
        if canonical_name(eff) == key:
            survivors.append(cik)
    return survivors[0] if len(survivors) == 1 else ""


def resolve_names(
    names: Iterable[str],
    index: dict[str, set[str]],
    *,
    cache: dict[str, str] | None = None,
    dates: dict[str, object] | None = None,
    fetcher: Fetcher | None = None,
) -> tuple[dict[str, str], list[dict], list[str]]:
    """Resolve names to CIKs via ``index`` (and a pinned ``cache``).

    Returns ``(resolved, collisions, unresolved)``. A name mapping to several
    CIKs is a collision unless ``dates[name]`` plus ``fetcher`` let the dated
    ``formerNames`` tie-breaker single out one CIK (then it is ``resolved``).
    Exact-after-normalization only -- no fuzzy matching.
    """
    cache = cache or {}
    dates = dates or {}
    resolved: dict[str, str] = {}
    collisions: list[dict] = []
    unresolved: list[str] = []
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        status, val = _resolve_one_name(name, index, cache)
        if status == "resolved":
            resolved[name] = val  # type: ignore[assignment]
        elif status == "collision":
            pick = ""
            target = _coerce_date(dates.get(name))
            if target is not None and fetcher is not None:
                pick = _tiebreak_collision_by_date(
                    canonical_name(name), val, target, fetcher)  # type: ignore[arg-type]
            if pick:
                resolved[name] = pick
            else:
                collisions.append({"name": name, "candidates": val})
        else:
            unresolved.append(name)
    return resolved, collisions, unresolved


def load_name_cache(path: Path | str) -> dict[str, str]:
    """Load the ``name,cik`` ledger CSV into ``{canonical_name: cik}``.

    Keys are re-canonicalized on load (idempotent) so a hand-edited file still
    matches queries. A CIK serialized as a float string (``"320193.0"``) is
    tolerated, mirroring :func:`load_cusip_crosswalk`.
    """
    import csv

    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    with p.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            raw_cik = (row.get("cik") or "").split(".")[0]
            key = canonical_name(name)
            if not key or not raw_cik.strip():
                continue
            try:
                out[key] = normalize_cik(raw_cik)
            except ValueError:
                continue
    return out


def write_name_cache(path: Path | str, pairs: Iterable[tuple[str, str]]) -> int:
    """Merge ``(name, cik)`` pairs into the ``name,cik`` ledger CSV (dedup).

    Names are canonicalized to the file's key form; the last CIK written for a
    key wins (a pin updates). Returns the total row count. Transparent
    optimization artifact -- written independently of ``--write``.
    """
    import csv

    p = Path(path)
    rows: dict[str, str] = {}
    if p.exists():
        rows.update(load_name_cache(p))
    for name, cik in pairs:
        key = canonical_name(name)
        if not key:
            continue
        try:
            rows[key] = normalize_cik(cik)
        except ValueError:
            continue
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "cik"])
        for key in sorted(rows):
            w.writerow([key, rows[key]])
    return len(rows)


def resolve_ciks(ciks: Iterable[str], fetcher: Fetcher) -> list[Issuer]:
    """Build issuers directly from CIKs via the submissions API.

    Unlike :func:`resolve_tickers`, this works for delisted / merged / renamed
    issuers that no longer appear in the current ticker map — the CIK is the
    permanent anchor. The (current) name and primary ticker are attached when
    available.
    """
    # Imported here to avoid a circular import at module load.
    from .sources.edgar_submissions import SUBMISSIONS_URL

    issuers: list[Issuer] = []
    for raw in ciks:
        cik = normalize_cik(raw)
        name, ticker = "", ""
        try:
            data = fetcher.get_json(SUBMISSIONS_URL.format(cik=cik))
            name = data.get("name", "")
            tks = data.get("tickers") or []
            ticker = tks[0] if tks else ""
        except Exception:  # noqa: BLE001 - keep the CIK even if metadata is unavailable
            pass
        issuers.append(Issuer(cik=cik, ticker=ticker, company=name))
    return issuers


def resolve_member_names(
    members: list[dict],
    name_index: dict[str, set[str]],
    *,
    name_cache: dict[str, str] | None = None,
    fetcher: Fetcher | None = None,
) -> tuple[list[dict], dict[str, str]]:
    """Fill ``cik`` on members still missing one via the name tier.

    Only members with an empty ``cik`` and a non-empty ``company`` are
    considered; their membership window (``first_seen`` preferred, else
    ``last_seen``) is the date hint for the collision tie-breaker. Mutates the
    member dicts in place and returns ``(members, resolved{company->cik})``.
    """
    pending = [m for m in members if not m.get("cik") and m.get("company")]
    if not pending:
        return members, {}
    names = [m["company"] for m in pending]
    dates = {m["company"]: (m.get("first_seen") or m.get("last_seen") or "")
             for m in pending}
    resolved, _collisions, _unresolved = resolve_names(
        names, name_index, cache=name_cache, dates=dates, fetcher=fetcher)
    for m in members:
        if not m.get("cik") and m.get("company") in resolved:
            m["cik"] = resolved[m["company"]]
    return members, resolved


def issuers_from_sp500(
    fetcher: Fetcher, *, start: str | None = None, current_only: bool = False,
    name_index: dict[str, set[str]] | None = None,
    name_cache: dict[str, str] | None = None,
) -> tuple[list[Issuer], list[dict], list[str]]:
    """Build an S&P 500 universe from Wikipedia composition.

    Returns ``(issuers, changes, unresolved)``. With ``current_only`` it's just
    today's members (CIK from the table). Otherwise it's the **historical union**
    over the window from ``start`` (every company that was a member), with dated
    membership (``first_seen``/``last_seen``) and the raw dated ``changes`` for
    point-in-time reconstruction. CIKs for since-removed members are resolved
    best-effort via the SEC ticker map; those still unresolved keep ``cik=""`` and
    are returned in ``unresolved``.
    """
    from .indices import sp500_current, sp500_membership

    if current_only:
        members = [{**m, "first_seen": "", "last_seen": "current"} for m in sp500_current(fetcher)]
        changes: list[dict] = []
    else:
        members, changes = sp500_membership(fetcher, start=start)

    # Resolve missing CIKs (since-removed members): first the SEC ticker map...
    need = [m["ticker"] for m in members if not m.get("cik")]
    if need:
        table = load_company_tickers(fetcher)
        for m in members:
            if not m.get("cik") and m["ticker"] in table:
                m["cik"] = table[m["ticker"]].cik

    # Capture members still without a CIK before the name tier runs, so that
    # ticker-resolved members sharing a company string with a name-resolved
    # member are not mislabeled "name" (dual-class shares: Fox A/B, etc.).
    cikless_before_name = {id(m) for m in members if not m.get("cik")}

    # ...then the name tier for whatever the ticker map still misses.
    name_resolved: dict[str, str] = {}
    if name_index is not None:
        members, name_resolved = resolve_member_names(
            members, name_index, name_cache=name_cache, fetcher=fetcher)

    issuers: list[Issuer] = []
    unresolved: list[str] = []
    for m in members:
        cik = m.get("cik") or ""
        if not cik:
            unresolved.append(m["ticker"])
        res = "name" if (cik and id(m) in cikless_before_name
                         and m.get("company") in name_resolved) else ""
        issuers.append(Issuer(cik=cik, ticker=m["ticker"], company=m.get("company", ""),
                              first_seen=m.get("first_seen", ""),
                              last_seen=m.get("last_seen", ""), resolution=res))
    return issuers, changes, unresolved


class Universe:
    """Load/save committed curated issuer lists under ``data/universe/``."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()

    def path(self, name: str) -> Path:
        return self.config.universe_dir / f"{name}.jsonl"

    def save(self, name: str, issuers: Iterable[Issuer]) -> Path:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # De-duplicate by CIK when present, else by ticker (unresolved members),
        # preserving order.
        seen: set[str] = set()
        with path.open("w", encoding="utf-8") as fh:
            for issuer in issuers:
                key = issuer.cik or f"ticker:{issuer.ticker}"
                if key in seen:
                    continue
                seen.add(key)
                fh.write(json.dumps(asdict(issuer), ensure_ascii=False) + "\n")
        return path

    def load(self, name: str) -> list[Issuer]:
        path = self.path(name)
        if not path.exists():
            raise FileNotFoundError(f"universe not found: {path}")
        issuers: list[Issuer] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            issuers.append(Issuer(
                cik=row.get("cik", ""), ticker=row.get("ticker", ""),
                company=row.get("company", ""),
                first_seen=row.get("first_seen", ""), last_seen=row.get("last_seen", ""),
                cusip6=row.get("cusip6", ""), resolution=row.get("resolution", ""),
            ))
        return issuers

    def names(self) -> list[str]:
        d = self.config.universe_dir
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.jsonl"))

    def iter_ciks(self, name: str) -> Iterator[str]:
        for issuer in self.load(name):
            if issuer.cik:  # skip unresolved (CIK-less) members
                yield issuer.cik
