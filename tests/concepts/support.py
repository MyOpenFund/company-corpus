"""Trimmed real SEC data for the concept tests: tests/fixtures/concepts/<name>.json."""
from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from company_corpus.concepts.summaries import select_for_f1

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "concepts"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def without_tag(facts: dict, tag: str) -> dict:
    """A deep copy of companyfacts with one tag gone."""
    out = json.loads(json.dumps(facts))
    for ns in out["facts"].values():
        ns.pop(tag, None)
    return out


def selected(name: str, *, statements=True, drop_tag: str | None = None) -> list[dict]:
    fx = fixture(name)
    facts = without_tag(fx["facts"], drop_tag) if drop_tag else fx["facts"]
    return select_for_f1(facts, fx["statements"] if statements else ()).rows


def pick(rows: list[dict], concept: str, day: str, frequency: str | None = None) -> dict | None:
    found = [r for r in rows if r["concept"] == concept and r["valid_date"].isoformat() == day
             and (frequency is None or r["frequency"] == frequency)]
    assert len(found) <= 1, found
    return found[0] if found else None


def millions(row: dict) -> Decimal:
    return row["value"] / Decimal(1000000)


__all__ = ["Counter", "fixture", "millions", "pick", "selected", "without_tag"]
