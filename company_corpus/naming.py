"""Point-in-time issuer naming.

A company's CIK is permanent, but its *name* changes (Facebook -> Meta, Google ->
Alphabet restructuring, countless mergers). EDGAR records this history in the
submissions API under ``formerNames``:

    "formerNames": [{"name": "Facebook Inc",
                     "from": "2005-05-06T04:00:00.000Z",
                     "to":   "2021-10-27T04:00:00.000Z"}]

This module resolves the name **in effect on a given filing date**, so a 2015
filing is attributed to "Facebook Inc" rather than the current "Meta Platforms,
Inc.". The current name is kept separately for search/joins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class NamePeriod:
    """A former name and the window during which it was in effect."""

    name: str
    start: date | None
    end: date | None


def _parse_edgar_dt(value: str | None) -> date | None:
    """Parse EDGAR's ``formerNames`` timestamps (ISO datetime, possibly ``Z``)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:  # pragma: no cover - defensive
            return None


def parse_former_names(former: list[dict] | None) -> list[NamePeriod]:
    """Turn the raw ``formerNames`` array into :class:`NamePeriod` objects."""
    periods: list[NamePeriod] = []
    for item in former or []:
        periods.append(
            NamePeriod(
                name=item.get("name", ""),
                start=_parse_edgar_dt(item.get("from")),
                end=_parse_edgar_dt(item.get("to")),
            )
        )
    return periods


def name_as_of(
    target: date | None, current_name: str, periods: list[NamePeriod]
) -> str:
    """Return the name in effect on ``target``.

    Falls back to ``current_name`` when the date is unknown or lies after the
    last former-name window (i.e. the company's present name applies).
    """
    if target is None:
        return current_name
    for period in periods:
        if not period.name:
            continue
        after_start = period.start is None or target >= period.start
        before_end = period.end is None or target <= period.end
        if after_start and before_end:
            return period.name
    return current_name


# Legal-form / article tokens dropped when canonicalizing a company name for
# name->CIK matching. Deliberately conservative: only legal forms and the
# article "THE" -- meaningful words (GROUP, HOLDINGS, FINANCIAL, ...) are kept
# because they distinguish issuers.
_LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COS",
    "LLC", "LLP", "LP", "PLC", "SA", "NV", "AG", "AB", "LTD", "LIMITED", "THE",
}


def canonical_name(name: str) -> str:
    """Strict canonical form of a company name for exact name->CIK matching.

    Upper-cases, turns every non-alphanumeric character into a space, drops
    legal-form suffix tokens (see ``_LEGAL_SUFFIXES``), and collapses
    whitespace. Idempotent, and applied symmetrically to index keys and query
    names so a match is exact-after-normalization. Returns ``""`` for a name
    made only of noise words.

    Note: ``_LEGAL_SUFFIXES`` tokens are dropped wherever they appear in the
    name (set-membership over all tokens, not trailing-suffix removal only).
    This is safe because the same canonicalization is applied symmetrically to
    both the index keys and every query name.
    """
    cleaned = "".join(ch if ch.isalnum() else " " for ch in str(name).upper())
    tokens = [t for t in cleaned.split() if t and t not in _LEGAL_SUFFIXES]
    return " ".join(tokens)
