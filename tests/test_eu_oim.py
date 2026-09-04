from company_corpus.xbrl import normalize_unit, normalize_period, flatten_oim_json


def test_normalize_unit():
    assert normalize_unit("iso4217:EUR") == "EUR"
    assert normalize_unit("iso4217:GBP/xbrli:shares") == "GBP/shares"
    assert normalize_unit("xbrli:shares") == "shares"
    assert normalize_unit("xbrli:pure") is None
    assert normalize_unit(None) is None


def test_normalize_period_canonical_midnight_shift():
    # A Dec-2020 year-end balance instant is canonically "2021-01-01T00:00:00".
    start, end, instant = normalize_period("2021-01-01T00:00:00")
    assert instant is True and start is None and end.isoformat() == "2020-12-31"
    # The matching FY2020 duration ends on the same midnight -> same conventional end.
    s, e, inst = normalize_period("2020-01-01T00:00:00/2021-01-01T00:00:00")
    assert inst is False and s.isoformat() == "2020-01-01" and e.isoformat() == "2020-12-31"


def test_flatten_drops_dimensioned_and_normalizes():
    report = {"facts": {
        "f1": {"value": 100, "dimensions": {
            "concept": "ifrs-full:Revenue", "entity": "x", "unit": "iso4217:EUR",
            "period": "2020-01-01T00:00:00/2021-01-01T00:00:00"}},
        "f2": {"value": 500, "dimensions": {
            "concept": "ifrs-full:Assets", "entity": "x", "unit": "iso4217:EUR",
            "period": "2021-01-01T00:00:00"}},
        "f3": {"value": 7, "dimensions": {   # dimensioned breakdown -> MUST be dropped
            "concept": "ifrs-full:Equity", "entity": "x", "unit": "iso4217:EUR",
            "period": "2021-01-01T00:00:00",
            "ifrs-full:ComponentsOfEquityAxis": "ifrs-full:IssuedCapitalMember"}},
    }}
    flat = flatten_oim_json(report, filed="2023-04-01", form="annual_report", accn="fxo-1")
    assert set(flat) == {"Revenue", "Assets"}            # f3 dropped
    rev = flat["Revenue"][0]
    assert rev["val"] == 100 and rev["unit"] == "EUR"
    assert rev["start"] == "2020-01-01" and rev["end"] == "2020-12-31"
    assert rev["filed"] == "2023-04-01" and rev["form"] == "annual_report" and rev["tag"] == "Revenue"
    assert "start" not in flat["Assets"][0] and flat["Assets"][0]["end"] == "2020-12-31"


def test_flatten_coerces_string_values_to_numbers():
    report = {"facts": {
        "rev": {"value": "16297000000", "dimensions": {
            "concept": "ifrs-full:Revenue", "entity": "x", "unit": "iso4217:EUR",
            "period": "2020-01-01T00:00:00/2021-01-01T00:00:00"}},
        "eps": {"value": "0.85", "dimensions": {
            "concept": "ifrs-full:BasicEarningsLossPerShare", "entity": "x",
            "unit": "iso4217:EUR/xbrli:shares",
            "period": "2020-01-01T00:00:00/2021-01-01T00:00:00"}},
    }}
    flat = flatten_oim_json(report, filed="2023-04-01", form="annual_report", accn="fxo-1")
    rev = flat["Revenue"][0]["val"]
    assert rev == 16297000000 and isinstance(rev, int)
    eps = flat["BasicEarningsLossPerShare"][0]["val"]
    assert eps == 0.85 and isinstance(eps, float)
