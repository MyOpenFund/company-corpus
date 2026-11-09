import copy
import json
from importlib import resources

import pytest

from company_corpus.concepts.dictionary import (
    DEFAULT_TABLES,
    DictionaryError,
    load_dictionary,
    parse_dictionary,
)

REVIEWED_NEW = {"long_term_debt_lines", "long_term_debt_incl_current", "payables_and_accrued",
                "receivables_total", "debt_total", "debt_components", "debt_current_total",
                "net_income_consolidated", "interest_net", "cash_and_st_investments"}


def _read(path: str) -> dict:
    return json.loads(resources.files("company_corpus.concepts").joinpath(path)
                      .read_text(encoding="utf-8"))


def _raw() -> tuple[dict, list[tuple[str, dict]]]:
    return _read("concepts.json"), [("us_gaap", _read("tags/us_gaap.json"))]


def _second_table() -> dict:
    return {"taxonomy": "ifrs-full", "namespace": "ifrs-full", "position_version_prefix": "ifrs/",
            "concepts": {"assets": {"status": "reviewed", "on_several": "first",
                                    "tags": ["Assets"], "statement_only_tags": []}}}


def test_the_packaged_dictionary_has_the_decided_shape():
    d = load_dictionary(("us_gaap",))
    assert d.version == "2.0.0" and d.conflict_tolerance == 0.01 and d.restatement_tolerance == 0.5
    assert len(d.concepts) == 71 and d.namespaces == ("us-gaap",)
    us = d.table("us-gaap")
    assert us.position_version_prefix == "us-gaap/" and len(us.entries) == 71
    reviewed = {k for k, e in us.entries.items() if e.status == "reviewed"}
    assert len(reviewed) == 48 and REVIEWED_NEW <= reviewed
    assert [k for k, e in us.entries.items() if e.on_several == "sum"] == [
        "long_term_debt_lines", "debt_components"]
    assert us.entries["liabilities"].computed.total_tag == "us-gaap:LiabilitiesAndStockholdersEquity"
    assert "dei:EntityCommonStockSharesOutstanding" in us.entries["shares_outstanding"].tags
    assert d.concepts["eps_basic"].kind == "money/shares" and d.concepts["cash"].kind == "money"


def test_every_tag_has_one_spec_keyed_by_full_tag_and_helpers_are_included():
    specs = load_dictionary(("us_gaap",)).tag_specs()
    assert specs["dei:EntityCommonStockSharesOutstanding"].ns == "dei"
    assert specs["dei:EntityCommonStockSharesOutstanding"].taxonomy_ns == "us-gaap"
    assert specs["us-gaap:EarningsPerShareBasic"].kind == "money/shares"
    assert specs["us-gaap:LiabilitiesAndStockholdersEquity"].instant is True
    assert specs["us-gaap:TemporaryEquityCarryingAmount"].kind == "money"
    assert specs["us-gaap:Assets"].full == "us-gaap:Assets"


def test_the_same_bare_tag_in_two_namespaces_is_two_specs():
    concepts, tables = _raw()
    d = parse_dictionary(concepts, [*tables, ("ifrs_full", _second_table())])
    specs = d.tag_specs()
    assert specs["us-gaap:Assets"].taxonomy_ns == "us-gaap"
    assert specs["ifrs-full:Assets"].taxonomy_ns == "ifrs-full"
    assert [t.namespace for t, _ in d.entries("assets")] == ["us-gaap", "ifrs-full"]
    assert d.entries("goodwill")[0][0].namespace == "us-gaap" and len(d.entries("goodwill")) == 1


def test_default_tables_are_loaded_by_default():
    assert load_dictionary().namespaces == tuple(
        load_dictionary(DEFAULT_TABLES).namespaces)


@pytest.mark.parametrize("change, message", [
    (lambda c, t: c["concepts"]["cash"].update(statement="XX"), "unknown statement"),
    (lambda c, t: c["concepts"]["cash"].update(kind="USD"), "kind"),
    (lambda c, t: c["concepts"]["cash"].update(instant="yes"), "instant"),
    (lambda c, t: c.update(version="one"), "version"),
    (lambda c, t: c.update(conflict_tolerance=-1), "conflict_tolerance"),
    (lambda c, t: c.update(restatement_tolerance="x"), "restatement_tolerance"),
    (lambda c, t: t["concepts"]["cash"].update(on_several="avg"), "on_several"),
    (lambda c, t: t["concepts"]["cash"].update(status="maybe"), "status"),
    (lambda c, t: t["concepts"]["cash"].update(tags=[], statement_only_tags=[]), "no tags"),
    (lambda c, t: t["concepts"]["goodwill"]["tags"].append("Assets"), "already in"),
    (lambda c, t: t["concepts"]["goodwill"]["tags"].append("ifrs-full:Goodwill"), "namespace"),
    (lambda c, t: t["concepts"].update(nope=t["concepts"]["cash"]), "unknown concept"),
    (lambda c, t: t["concepts"]["liabilities"]["computed"]["minus_first_of"].append("nope"),
     "unknown concept"),
    (lambda c, t: t.update(namespace=""), "namespace"),
    (lambda c, t: t.update(position_version_prefix=""), "position_version_prefix"),
])
def test_a_bad_dictionary_is_refused(change, message):
    concepts, tables = copy.deepcopy(_raw())
    change(concepts, tables[0][1])
    with pytest.raises(DictionaryError, match=message):
        parse_dictionary(concepts, tables)


@pytest.mark.parametrize("change, message", [
    (lambda t: t["concepts"]["assets"].update(tags=["us-gaap:Assets"]), "already in"),
    (lambda t: t.update(namespace="us-gaap"), "namespace us-gaap"),
    (lambda t: t.update(position_version_prefix="us-gaap/"), "position_version_prefix"),
])
def test_two_tables_must_not_overlap(change, message):
    concepts, tables = copy.deepcopy(_raw())
    second = _second_table()
    change(second)
    with pytest.raises(DictionaryError, match=message):
        parse_dictionary(concepts, [*tables, ("ifrs_full", second)])


def test_an_error_names_the_table():
    concepts, tables = copy.deepcopy(_raw())
    tables[0][1]["concepts"]["cash"]["status"] = "maybe"
    with pytest.raises(DictionaryError, match=r"us_gaap: cash"):
        parse_dictionary(concepts, tables)
