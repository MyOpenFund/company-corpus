"""A dictionary with a small synthetic IFRS table, for tests that must not depend on the real one."""
from __future__ import annotations

import json
from importlib import resources

from company_corpus.concepts.dictionary import Dictionary, parse_dictionary

TEST_IFRS_TABLE = {
    "taxonomy": "ifrs-full", "namespace": "ifrs-full", "position_version_prefix": "ifrs/",
    "concepts": {
        "revenue": {"status": "reviewed", "on_several": "first", "tags": ["Revenue"],
                    "statement_only_tags": []},
        "net_income": {"status": "reviewed", "on_several": "first",
                       "tags": ["ProfitLossAttributableToOwnersOfParent"], "statement_only_tags": []},
        "eps_basic": {"status": "reviewed", "on_several": "first",
                      "tags": ["BasicEarningsLossPerShare"], "statement_only_tags": []},
        "assets": {"status": "reviewed", "on_several": "first", "tags": ["Assets"],
                   "statement_only_tags": []},
        "liabilities": {"status": "reviewed", "on_several": "first", "tags": ["Liabilities"],
                        "statement_only_tags": []},
        "equity": {"status": "reviewed", "on_several": "first",
                   "tags": ["EquityAttributableToOwnersOfParent"], "statement_only_tags": []},
        "debt_components": {"status": "reviewed", "on_several": "first",
                            "tags": ["Borrowings"], "statement_only_tags": []},
    },
}


def _read(path: str) -> dict:
    return json.loads(resources.files("company_corpus.concepts").joinpath(path)
                      .read_text(encoding="utf-8"))


def with_test_ifrs() -> Dictionary:
    """Real concepts, real us-gaap table, then the synthetic IFRS table."""
    return parse_dictionary(_read("concepts.json"),
                            [("us_gaap", _read("tags/us_gaap.json")), ("ifrs_full", TEST_IFRS_TABLE)])


def companyfacts(points: dict[str, list[dict]]) -> dict:
    """``{"ns:Tag": [{"unit": ..., <companyfacts point>}]}`` -> a companyfacts document."""
    facts: dict[str, dict] = {}
    for full, pts in points.items():
        ns, tag = full.split(":", 1)
        units: dict[str, list[dict]] = {}
        for p in pts:
            q = dict(p)
            units.setdefault(q.pop("unit"), []).append(q)
        facts.setdefault(ns, {})[tag] = {"label": tag, "units": units}
    return {"cik": 1, "entityName": "Test", "facts": facts}
