from company_corpus.financials import CONCEPTS_BY_KEY
from company_corpus.xbrl import IFRS_CONCEPTS, IFRS_CONCEPTS_BY_KEY


def test_keys_are_a_subset_of_the_sec_keys():
    # Every IFRS key must reuse a SEC curated key, so the shared engine + derived
    # metrics line up and the two pillars are directly comparable.
    assert set(IFRS_CONCEPTS_BY_KEY) <= set(CONCEPTS_BY_KEY)


def test_headline_mappings_present():
    assert IFRS_CONCEPTS_BY_KEY["revenue"].tags[0] == "Revenue"
    assert IFRS_CONCEPTS_BY_KEY["net_income"].tags == ("ProfitLossAttributableToOwnersOfParent", "ProfitLoss")
    assert IFRS_CONCEPTS_BY_KEY["assets"].instant is True
    assert IFRS_CONCEPTS_BY_KEY["equity"].tags[0] == "EquityAttributableToOwnersOfParent"
    assert all(c.tags for c in IFRS_CONCEPTS)  # no empty fallback lists
