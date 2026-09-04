"""S&P 500 index composition — current members and dated historical changes.

Index membership is proprietary in general, but the **S&P 500** has an open,
dated history on Wikipedia: a current-constituents table (with a ``CIK`` column)
plus a **changes table** (effective date, added/removed ticker + security). From
those two we reconstruct point-in-time membership and the union of every company
that was a member over a window — so a universe built from this is not
survivorship-biased on the selection side.

Only S&P 500 is supported for now: Russell 1000 / Nasdaq-100 have no open dated
composition source (their Wikipedia constituent tables are recent-only).

Note: the membership *timeline* (ticker / company / dates) is exact. CIKs are
reliable for current members (from the table); CIKs for since-removed members are
best-effort (the live SEC ticker map is current-only and reuses symbols).
"""

from __future__ import annotations

import io
import re

import pandas as pd
from dateutil import parser as dateparser

from .config import normalize_cik
from .http import Fetcher

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def _norm_ticker(value) -> str:
    """Normalize a ticker: upper, strip footnotes, Wikipedia ``BRK.B`` -> ``BRK-B``."""
    if value is None:
        return ""
    s = re.sub(r"[^A-Z0-9.\- ]", "", str(value).upper()).strip()
    s = s.split(" ")[0] if s else ""
    return s.replace(".", "-")


def _parse_date(value) -> str:
    try:
        return dateparser.parse(str(value)).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return ""


def _read_tables(fetcher: Fetcher, url: str = SP500_URL) -> list[pd.DataFrame]:
    return pd.read_html(io.StringIO(fetcher.get_text(url)))


def _col(df: pd.DataFrame, *needles: str):
    """Return the first column whose flattened name contains all needles (ci)."""
    for c in df.columns:
        name = " ".join(str(p) for p in c) if isinstance(c, tuple) else str(c)
        low = name.lower()
        if all(n in low for n in needles):
            return c
    return None


def _constituents_table(tabs: list[pd.DataFrame]) -> pd.DataFrame:
    for t in tabs:
        if _col(t, "symbol") is not None and _col(t, "cik") is not None:
            return t
    # Fallback: largest table with a Symbol/Ticker column.
    cands = [t for t in tabs if _col(t, "symbol") is not None or _col(t, "ticker") is not None]
    if not cands:
        raise ValueError("S&P 500 constituents table not found on the page")
    return max(cands, key=len)


def _changes_table(tabs: list[pd.DataFrame]) -> pd.DataFrame | None:
    for t in tabs:
        if _col(t, "added", "ticker") is not None and _col(t, "removed", "ticker") is not None:
            return t
    return None


def sp500_current(fetcher: Fetcher) -> list[dict]:
    """Current S&P 500 members: ``[{ticker, company, cik}]`` (CIK from the table)."""
    df = _constituents_table(_read_tables(fetcher))
    sym, cik_c, sec = _col(df, "symbol") or _col(df, "ticker"), _col(df, "cik"), _col(df, "security")
    out = []
    for _, r in df.iterrows():
        ticker = _norm_ticker(r[sym])
        if not ticker:
            continue
        cik = ""
        if cik_c is not None and pd.notna(r[cik_c]):
            try:
                cik = normalize_cik(r[cik_c])
            except ValueError:
                cik = ""
        out.append({"ticker": ticker, "company": str(r[sec]).strip() if sec is not None else "", "cik": cik})
    return out


def sp500_changes(fetcher: Fetcher) -> list[dict]:
    """Dated add/remove events: ``[{date, added, added_company, removed, removed_company}]``."""
    tabs = _read_tables(fetcher)
    df = _changes_table(tabs)
    if df is None:
        return []
    dcol = _col(df, "date")
    at, asec = _col(df, "added", "ticker"), _col(df, "added", "security")
    rt, rsec = _col(df, "removed", "ticker"), _col(df, "removed", "security")
    out = []
    for _, r in df.iterrows():
        d = _parse_date(r[dcol]) if dcol is not None else ""
        out.append({
            "date": d,
            "added": _norm_ticker(r[at]) if at is not None else "",
            "added_company": str(r[asec]).strip() if asec is not None and pd.notna(r[asec]) else "",
            "removed": _norm_ticker(r[rt]) if rt is not None else "",
            "removed_company": str(r[rsec]).strip() if rsec is not None and pd.notna(r[rsec]) else "",
        })
    return out


def sp500_membership(fetcher: Fetcher, start: str | None = None) -> tuple[list[dict], list[dict]]:
    """Reconstruct the union of members over a window + the dated changes.

    Returns ``(members, changes)`` where ``members`` is
    ``[{ticker, company, cik, first_seen, last_seen}]`` — the union of the current
    constituents and every company named in the changes table from ``start`` (ISO
    date or ``YYYY``) to today. This is best-effort, not exhaustive: a company that
    joined and left entirely before the earliest change row (so it appears in
    neither source) is not recovered. ``last_seen="current"`` for present members;
    otherwise the last removal date. ``cik`` is filled for current members (from the
    table) and left ``""`` for since-removed ones (resolved best-effort by the caller).
    """
    if start and len(start) == 4:
        start = f"{start}-01-01"
    current = sp500_current(fetcher)
    changes = sp500_changes(fetcher)
    cur = {c["ticker"]: c for c in current}

    # Per-ticker add/remove dates + a company-name hint.
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    name: dict[str, str] = {}
    for ch in changes:
        if ch["added"]:
            added.setdefault(ch["added"], []).append(ch["date"])
            name.setdefault(ch["added"], ch["added_company"])
        if ch["removed"]:
            removed.setdefault(ch["removed"], []).append(ch["date"])
            name.setdefault(ch["removed"], ch["removed_company"])

    tickers = set(cur) | set(added) | set(removed)
    members: list[dict] = []
    for t in sorted(tickers):
        add_dates = sorted(d for d in added.get(t, []) if d)
        rem_dates = sorted(d for d in removed.get(t, []) if d)
        first_seen = add_dates[0] if add_dates else ""
        last_seen = "current" if t in cur else (rem_dates[-1] if rem_dates else "")
        # Window filter: keep current members, or any with activity on/after start.
        if start and t not in cur:
            latest = max(add_dates + rem_dates) if (add_dates or rem_dates) else ""
            if latest and latest < start:
                continue
        members.append({
            "ticker": t,
            "company": cur[t]["company"] if t in cur else name.get(t, ""),
            "cik": cur[t]["cik"] if t in cur else "",
            "first_seen": first_seen,
            "last_seen": last_seen,
        })
    return members, changes
