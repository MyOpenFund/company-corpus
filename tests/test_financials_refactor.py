from company_corpus.financials import (
    CONCEPTS, build_period_summaries, summaries_from_flat, flatten_points,
)

FACTS = {"facts": {"us-gaap": {
    "Revenues": {"label": "Revenue", "units": {"USD": [
        {"val": 100, "start": "2022-01-01", "end": "2022-12-31",
         "filed": "2023-02-01", "form": "10-K", "accn": "a1"}]}},
    "Assets": {"label": "Assets", "units": {"USD": [
        {"val": 500, "end": "2022-12-31",
         "filed": "2023-02-01", "form": "10-K", "accn": "a1"}]}},
}}}


def test_refactor_default_matches_explicit_pack():
    a = build_period_summaries(FACTS, company="X", company_current="X")
    b = build_period_summaries(FACTS, company="X", company_current="X", concepts=CONCEPTS)
    assert [s.values for s in a] == [s.values for s in b]


def test_summaries_from_flat_is_the_engine_core():
    flat = flatten_points(FACTS)
    out = summaries_from_flat(flat, concepts=CONCEPTS, company="X", company_current="X")
    assert len(out) == 1
    s = out[0]
    assert s.period_end.isoformat() == "2022-12-31"
    assert s.values["revenue"]["value"] == 100
    assert s.values["assets"]["value"] == 500
