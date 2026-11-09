"""Per-concept coverage on an issuer's latest annual period, for the run report (spec §9)."""
from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable

from .dictionary import Dictionary, load_dictionary

STATES = ("statement", "note", "inferred", "unknown", "derived", "absent", "conflict",
          "on_statement_no_value")


def positioned_tags(positions: Iterable[tuple[str, str, str | None]], d: Dictionary) -> set[tuple[str, str]]:
    """``(filing_ref, "ns:Tag")`` for each ``(filing_ref, tag, tag_version)`` position whose version
    names a loaded taxonomy; extension elements (version = an accession) are left out."""
    prefixes = [(t.position_version_prefix, t.namespace) for t in d.tables]
    out: set[tuple[str, str]] = set()
    for filing_ref, tag, version in positions:
        for prefix, ns in prefixes:
            if (version or "").startswith(prefix):
                out.add((filing_ref, f"{ns}:{tag}"))
                break
    return out


def _own_tags(d: Dictionary, key: str) -> list[str]:
    """The concept's tags in every table; a helper-namespace tag (``dei:``) is never on a
    statement, so it is counted under its table's namespace, as the bare name was before."""
    out = []
    for table, e in d.entries(key):
        for t in e.all_tags:
            ns, tag = t.split(":", 1)
            out.append(t if ns == table.namespace else f"{table.namespace}:{tag}")
    return out


def coverage(rows: list[dict], on_statement: Collection[tuple[str, str]],
             dictionary: Dictionary | None = None) -> Counter:
    """``coverage:<concept>:<state>`` counts. ``rows`` is one issuer's selection output
    and ``on_statement`` its ``(filing_ref, "ns:Tag")`` pairs (see ``positioned_tags``).
    ``on_statement_no_value`` is a chain tag on the reference filing's statements with no plain value: dimension-only lines, which
    companyfacts does not carry."""
    d = dictionary or load_dictionary()
    annual = [r for r in rows if r["frequency"] == "annual"]
    if not annual:
        return Counter({"coverage:no_annual_period": 1})
    end = max(r["valid_date"] for r in annual)
    at_end = [r for r in rows if r["valid_date"] == end and r["frequency"] in ("annual", "instant")]
    reference = max(at_end, key=lambda r: (r["known_at"], r["filing_ref"] or ""))["filing_ref"]
    found = {r["concept"]: r for r in at_end}
    out: Counter = Counter()
    for key in d.concepts:
        row = found.get(key)
        if row is None:
            out[f"coverage:{key}:absent"] += 1
            if any((reference, t) in on_statement for t in _own_tags(d, key)):
                out[f"coverage:{key}:on_statement_no_value"] += 1
            continue
        out[f"coverage:{key}:{row['location']}"] += 1
        if row["conflict"]:
            out[f"coverage:{key}:conflict"] += 1
    return out


def format_coverage(counts: Counter, dictionary: Dictionary | None = None) -> Iterable[str]:
    """One line per concept, in dictionary order."""
    d = dictionary or load_dictionary()
    if counts.get("coverage:no_annual_period"):
        yield f"coverage: {counts['coverage:no_annual_period']} issuer(s) with no annual period"
    if counts.get("statements_missing"):
        yield f"statements_missing: {counts['statements_missing']} issuer(s) without statement positions"
    for key in d.concepts:
        parts = [f"{s}={counts[f'coverage:{key}:{s}']}" for s in STATES
                 if counts.get(f"coverage:{key}:{s}")]
        if parts:
            yield f"coverage {key}: " + " ".join(parts)
