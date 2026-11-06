import copy
import json
from importlib import resources

import pytest

from company_corpus.concepts.dictionary import DictionaryError, load_dictionary, parse_dictionary

REVIEWED_NEW = {"payables_and_accrued", "receivables_total", "debt_total", "debt_components",
                "debt_current_total", "net_income_consolidated", "interest_net",
                "cash_and_st_investments"}


def _raw() -> dict:
    return json.loads(resources.files("company_corpus.concepts").joinpath("us_gaap.json")
                      .read_text(encoding="utf-8"))


def test_the_packaged_dictionary_has_the_decided_shape():
    d = load_dictionary()
    assert d.version == "1.0.0" and d.taxonomy == "us-gaap" and d.conflict_tolerance == 0.01
    assert d.restatement_tolerance == 0.5
    assert len(d.concepts) == 69
    reviewed = {k for k, c in d.concepts.items() if c.status == "reviewed"}
    assert len(reviewed) == 46 and REVIEWED_NEW <= reviewed
    assert [k for k, c in d.concepts.items() if c.on_several == "sum"] == ["debt_components"]
    assert d.concepts["liabilities"].computed.total_tag == "us-gaap:LiabilitiesAndStockholdersEquity"
    assert "dei:EntityCommonStockSharesOutstanding" in d.concepts["shares_outstanding"].tags


def test_every_tag_has_one_spec_and_helpers_are_included():
    specs = load_dictionary().tag_specs()
    assert specs["EntityCommonStockSharesOutstanding"].ns == "dei"
    assert specs["EarningsPerShareBasic"].unit == "USD/shares"
    assert specs["LiabilitiesAndStockholdersEquity"].instant is True
    assert specs["TemporaryEquityCarryingAmount"].unit == "USD"


@pytest.mark.parametrize("change, message", [
    (lambda r: r["concepts"]["cash"].update(statement="XX"), "unknown statement"),
    (lambda r: r["concepts"]["cash"].update(on_several="avg"), "on_several"),
    (lambda r: r["concepts"]["cash"].update(status="maybe"), "status"),
    (lambda r: r["concepts"]["cash"].update(unit="EUR"), "unit"),
    (lambda r: r["concepts"]["cash"].update(tags=[], statement_only_tags=[]), "no tags"),
    (lambda r: r["concepts"]["goodwill"]["tags"].append("Assets"), "already in"),
    (lambda r: r["concepts"]["liabilities"]["computed"]["minus_first_of"].append("nope"), "unknown concept"),
    (lambda r: r.update(version="one"), "version"),
    (lambda r: r.update(conflict_tolerance=-1), "conflict_tolerance"),
    (lambda r: r.update(restatement_tolerance="x"), "restatement_tolerance"),
    (lambda r: r.update(namespace=""), "namespace"),
])
def test_a_bad_dictionary_is_refused(change, message):
    raw = copy.deepcopy(_raw())
    change(raw)
    with pytest.raises(DictionaryError, match=message):
        parse_dictionary(raw)
