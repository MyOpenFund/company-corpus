"""Per-concept coverage on an issuer's latest annual period, for the run report (spec §9)."""
from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable

from .dictionary import Dictionary, load_dictionary

STATES = ("statement", "note", "inferred", "unknown", "derived", "absent", "conflict",
          "on_statement_no_value")


def coverage(rows: list[dict], on_statement: Collection[tuple[str, str]],
             dictionary: Dictionary | None = None) -> Counter:
    """``coverage:<concept>:<state>`` counts. ``rows`` is one issuer's selection output and
    ``on_statement`` its ``(filing_ref, tag)`` pairs. ``on_statement_no_value`` is a chain tag on
    the reference filing's statements with no plain value: dimension-only lines, which
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
    for key, c in d.concepts.items():
        row = found.get(key)
        if row is None:
            out[f"coverage:{key}:absent"] += 1
            if any((reference, t.split(":", 1)[1]) in on_statement
                   for t in (*c.tags, *c.statement_only_tags)):
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
