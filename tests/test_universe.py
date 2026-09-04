from __future__ import annotations

import pytest

from company_corpus.universe import (
    Issuer,
    Universe,
    issuers_from_sp500,
    load_company_tickers,
    load_cusip_crosswalk,
    load_name_cache,
    read_identifier_csv,
    reconcile_identifiers,
    resolve_ciks,
    resolve_cusips,
    resolve_member_names,
    resolve_names,
    resolve_tickers,
    write_cusip_crosswalk,
    write_name_cache,
)


def test_resolve_tickers(apple_fetcher):
    issuers, unresolved = resolve_tickers(["aapl", "MSFT", "NOPE"], apple_fetcher)
    assert unresolved == ["NOPE"]
    by_ticker = {i.ticker: i for i in issuers}
    assert by_ticker["AAPL"].cik == "0000320193"
    assert by_ticker["MSFT"].cik == "0000789019"
    assert by_ticker["AAPL"].company == "Apple Inc."


def test_issuer_normalizes_cik():
    assert Issuer(cik=320193, ticker="AAPL").cik == "0000320193"


def test_universe_save_load_roundtrip(config):
    uni = Universe(config)
    issuers = [Issuer(cik="320193", ticker="AAPL", company="Apple Inc."),
               Issuer(cik="789019", ticker="MSFT", company="MICROSOFT CORP")]
    path = uni.save("curated", issuers)
    assert path.exists()
    loaded = uni.load("curated")
    assert [i.cik for i in loaded] == ["0000320193", "0000789019"]
    assert uni.names() == ["curated"]
    assert list(uni.iter_ciks("curated")) == ["0000320193", "0000789019"]


def test_universe_dedup_on_save(config):
    uni = Universe(config)
    uni.save("dup", [Issuer(cik="320193", ticker="AAPL"), Issuer(cik="320193", ticker="AAPL")])
    assert len(uni.load("dup")) == 1


def test_load_company_tickers_collision_prefers_lowest_cik(make_fetcher):
    # A ticker mapped to two different CIKs must resolve deterministically (lowest
    # CIK), independent of feed order, and surface a warning -- not last-write-wins.
    routes = {"company_tickers.json": {
        "0": {"cik_str": 789019, "ticker": "DUP", "title": "Higher CIK first"},
        "1": {"cik_str": 320193, "ticker": "DUP", "title": "Lower CIK second"},
        "2": {"cik_str": 111, "ticker": "AAPL", "title": "Apple"},
    }}
    fetcher = make_fetcher(routes)
    with pytest.warns(UserWarning, match="multiple"):
        out = load_company_tickers(fetcher)
    assert out["DUP"].cik == "0000320193"  # lowest CIK wins regardless of order
    assert out["AAPL"].cik == "0000000111"


def test_load_company_tickers_no_warning_when_clean(make_fetcher):
    routes = {"company_tickers.json": {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    }}
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning would fail the test
        out = load_company_tickers(make_fetcher(routes))
    assert out["AAPL"].cik == "0000320193"


def test_resolve_ciks_uses_submissions(apple_fetcher):
    # CIK-anchored: resolves via submissions API, and keeps a CIK even when its
    # metadata is unavailable (delisted/merged issuer not in the ticker map).
    issuers = resolve_ciks(["320193", "999999"], apple_fetcher)
    by_cik = {i.cik: i for i in issuers}
    assert by_cik["0000320193"].company == "Apple Inc."
    assert by_cik["0000320193"].ticker == "AAPL"
    assert "0000999999" in by_cik
    assert by_cik["0000999999"].company == ""  # no route -> empty, but CIK retained


def test_issuer_defaults_cusip_fields_empty():
    issuer = Issuer(cik="320193", ticker="AAPL")
    assert issuer.cusip6 == ""
    assert issuer.resolution == ""


def test_universe_roundtrips_cusip6_and_resolution(config):
    uni = Universe(config)
    uni.save("withcusip", [Issuer(cik="320193", ticker="AAPL", company="Apple Inc.",
                                  cusip6="037833", resolution="both")])
    loaded = uni.load("withcusip")
    assert loaded[0].cusip6 == "037833"
    assert loaded[0].resolution == "both"


SAMPLE_CROSSWALK = """cik,cusip6,cusip8
320193.0,037833,03783310
789019.0,594918,59491810
12345.0,DUPDUP,DUPDUP10
67890.0,DUPDUP,DUPDUP20
"""


def _write_crosswalk(tmp_path):
    p = tmp_path / "xw.csv"
    p.write_text(SAMPLE_CROSSWALK, encoding="utf-8")
    return p


def test_load_cusip_crosswalk_normalizes_cik_despite_float_artifact(tmp_path):
    xw = load_cusip_crosswalk(_write_crosswalk(tmp_path))
    assert xw["037833"] == {"0000320193"}
    assert xw["594918"] == {"0000789019"}


def test_load_cusip_crosswalk_collects_multiple_ciks_per_cusip6(tmp_path):
    xw = load_cusip_crosswalk(_write_crosswalk(tmp_path))
    assert xw["DUPDUP"] == {"0000012345", "0000067890"}


def test_resolve_cusips_single_match(tmp_path):
    xw = load_cusip_crosswalk(_write_crosswalk(tmp_path))
    resolved, unresolved = resolve_cusips(["037833", "594918"], xw)
    assert resolved == {"037833": "0000320193", "594918": "0000789019"}
    assert unresolved == []


def test_resolve_cusips_absent_goes_unresolved(tmp_path):
    xw = load_cusip_crosswalk(_write_crosswalk(tmp_path))
    resolved, unresolved = resolve_cusips(["999999"], xw)
    assert resolved == {} and unresolved == ["999999"]


def test_resolve_cusips_ambiguous_multi_cik_is_unresolved(tmp_path):
    xw = load_cusip_crosswalk(_write_crosswalk(tmp_path))
    with pytest.warns(UserWarning, match="multiple CIKs"):
        resolved, unresolved = resolve_cusips(["DUPDUP"], xw)
    assert resolved == {} and unresolved == ["DUPDUP"]


TICKER_TABLE = {
    "AAPL": Issuer(cik="320193", ticker="AAPL", company="Apple Inc."),
    "DT": Issuer(cik="1773383", ticker="DT", company="Dynatrace, Inc."),
    "KO": Issuer(cik="21344", ticker="KO", company="COCA COLA CO"),
}
CROSSWALK = {
    "037833": {"0000320193"},
    "25156P": {"0000999999"},
    "191216": {"0000888888"},
}


def test_reconcile_provided_cik_is_authoritative():
    rows = [{"cik": "1750", "ticker": "AAPL", "cusip6": "037833", "name": "AAR Corp"}]
    issuers, collisions, unresolved = reconcile_identifiers(rows, TICKER_TABLE, CROSSWALK)
    assert collisions == [] and unresolved == []
    assert issuers[0].cik == "0000001750"
    assert issuers[0].resolution == "cik"


def test_reconcile_both_sources_agree():
    rows = [{"cik": "", "ticker": "AAPL", "cusip6": "037833", "name": "Apple Inc."}]
    issuers, collisions, unresolved = reconcile_identifiers(rows, TICKER_TABLE, CROSSWALK)
    assert collisions == [] and unresolved == []
    assert issuers[0].cik == "0000320193" and issuers[0].resolution == "both"


def test_reconcile_flags_homonym_collision():
    rows = [{"cik": "", "ticker": "DT", "cusip6": "25156P", "name": "Deutsche Telekom Intl"}]
    issuers, collisions, unresolved = reconcile_identifiers(rows, TICKER_TABLE, CROSSWALK)
    assert issuers == []
    assert collisions[0]["ticker"] == "DT"
    assert collisions[0]["cik_ticker"] == "0001773383"
    assert collisions[0]["cik_cusip"] == "0000999999"
    assert collisions[0]["kind"] == "name_mismatch"
    assert collisions[0]["sec_ticker_name"] == "Dynatrace, Inc."


def test_reconcile_classifies_collision_name_match():
    rows = [{"cik": "", "ticker": "KO", "cusip6": "191216", "name": "The Coca-Cola Company"}]
    _, collisions, _ = reconcile_identifiers(rows, TICKER_TABLE, CROSSWALK)
    assert collisions[0]["kind"] == "name_match"


def test_reconcile_ticker_only_and_cusip_only_and_unresolved():
    rows = [
        {"cik": "", "ticker": "AAPL", "cusip6": "ZZZZZZ", "name": ""},
        {"cik": "", "ticker": "TKM", "cusip6": "25156P", "name": "DT Fin"},
        {"cik": "", "ticker": "NOPE", "cusip6": "ZZZZZZ", "name": "Mystery"},
    ]
    issuers, collisions, unresolved = reconcile_identifiers(rows, TICKER_TABLE, CROSSWALK)
    assert {i.resolution for i in issuers} == {"ticker", "cusip"}
    assert {i.cik for i in issuers} == {"0000320193", "0000999999"}
    assert unresolved == ["Mystery"]


def test_read_identifier_csv_autodetects_cik_ticker_cusip(tmp_path):
    csv_path = tmp_path / "u.csv"
    csv_path.write_text(
        "CIK,Ticker,CUSIP,Issuer\n"
        "0000320193,AAPL,037833AA0,Apple Inc\n"
        "0000320193,AAPL,037833BB1,Apple Inc\n"
        ",AFL,001055AY8,Aflac Inc\n",
        encoding="utf-8",
    )
    rows = read_identifier_csv(csv_path)
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["AAPL"]["cik"] == "0000320193"
    assert by_ticker["AAPL"]["cusip6"] == "037833"
    assert by_ticker["AFL"]["cik"] == ""
    assert by_ticker["AFL"]["cusip6"] == "001055"


def test_read_identifier_csv_derives_cusip6_from_isin(tmp_path):
    csv_path = tmp_path / "u.csv"
    csv_path.write_text("Ticker,ISIN\nABBV,US00287YAD56\n", encoding="utf-8")
    rows = read_identifier_csv(csv_path)
    assert rows[0]["cusip6"] == "00287Y"


def test_read_identifier_csv_keeps_full_cusip(tmp_path):
    csv_path = tmp_path / "u.csv"
    csv_path.write_text(
        "Ticker,CUSIP\n"
        "AAPL,037833AA0\n"
        "AAPL,037833AA0\n"   # most common full CUSIP for AAPL
        "AAPL,037833BB1\n",
        encoding="utf-8",
    )
    rows = read_identifier_csv(csv_path)
    assert rows[0]["cusip"] == "037833AA0"
    assert rows[0]["cusip6"] == "037833"


class _FakeFTS:
    """resolve(cusip) -> (cik, name) from a dict; counts calls."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = 0

    def resolve(self, cusip):
        self.calls += 1
        return self.mapping.get(cusip)


def test_reconcile_fts_confirmed_when_name_corroborates():
    rows = [{"cik": "", "ticker": "TKM", "cusip6": "25156P", "cusip": "25156PAA0",
             "name": "Deutsche Telekom Intl Finance"}]
    fts = _FakeFTS({"25156PAA0": ("0000999999", "DEUTSCHE TELEKOM INTL FIN BV")})
    issuers, _, unresolved = reconcile_identifiers(rows, TICKER_TABLE, {}, fts=fts)
    assert unresolved == []
    assert issuers[0].cik == "0000999999"
    assert issuers[0].resolution == "fts:confirmed"


def test_reconcile_fts_unverified_when_name_differs():
    rows = [{"cik": "", "ticker": "TKM", "cusip6": "25156P", "cusip": "25156PAA0",
             "name": "Deutsche Telekom Intl Finance"}]
    fts = _FakeFTS({"25156PAA0": ("0000999999", "SOME UNDERWRITER LLC")})
    issuers, _, _ = reconcile_identifiers(rows, TICKER_TABLE, {}, fts=fts)
    assert issuers[0].resolution == "fts:unverified"


def test_reconcile_fts_no_hit_stays_unresolved():
    rows = [{"cik": "", "ticker": "NOPE", "cusip6": "ZZZZZZ", "cusip": "ZZZZZZZZ9",
             "name": "Mystery"}]
    fts = _FakeFTS({})
    issuers, _, unresolved = reconcile_identifiers(rows, TICKER_TABLE, {}, fts=fts)
    assert issuers == [] and unresolved == ["Mystery"]


def test_reconcile_fts_limit_caps_calls():
    rows = [{"cik": "", "ticker": f"T{i}", "cusip6": "ZZZZZZ", "cusip": f"ZZZZZZ{i:02d}9",
             "name": f"co{i}"} for i in range(5)]
    fts = _FakeFTS({})
    reconcile_identifiers(rows, TICKER_TABLE, {}, fts=fts, fts_limit=2)
    assert fts.calls == 2


def test_reconcile_without_fts_is_unchanged():
    rows = [{"cik": "", "ticker": "NOPE", "cusip6": "ZZZZZZ", "cusip": "ZZZZZZZZ9",
             "name": "Mystery"}]
    issuers, collisions, unresolved = reconcile_identifiers(rows, TICKER_TABLE, {})
    assert issuers == [] and collisions == [] and unresolved == ["Mystery"]


def test_write_cusip_crosswalk_roundtrips(tmp_path):
    path = tmp_path / "cache.csv"
    n = write_cusip_crosswalk(path, [("320193", "037833"), ("789019", "594918")])
    assert n == 2
    assert load_cusip_crosswalk(path) == {"037833": {"0000320193"}, "594918": {"0000789019"}}


def test_write_cusip_crosswalk_merges_and_dedups(tmp_path):
    path = tmp_path / "cache.csv"
    write_cusip_crosswalk(path, [("320193", "037833")])
    n = write_cusip_crosswalk(path, [("0000320193", "037833"), ("789019", "594918")])
    assert n == 2
    assert load_cusip_crosswalk(path) == {"037833": {"0000320193"}, "594918": {"0000789019"}}


def test_resolve_names_unique_collision_unresolved():
    index = {"WIDGET": {"0000999999"}, "SUNRISE": {"0000111111", "0000222222"}}
    resolved, collisions, unresolved = resolve_names(
        ["Widget Inc", "Sunrise Corp", "Nobody LLC"], index)
    assert resolved == {"Widget Inc": "0000999999"}
    assert collisions == [{"name": "Sunrise Corp",
                           "candidates": ["0000111111", "0000222222"]}]
    assert unresolved == ["Nobody LLC"]


def test_resolve_names_cache_short_circuits_index_and_collision():
    index = {"SUNRISE": {"0000111111", "0000222222"}}
    cache = {"SUNRISE": "0000111111"}  # pinned decision, keyed by canonical name
    resolved, collisions, unresolved = resolve_names(
        ["Sunrise Corp"], index, cache=cache)
    assert resolved == {"Sunrise Corp": "0000111111"}
    assert collisions == [] and unresolved == []


def test_name_cache_roundtrip_merges_and_dedups(tmp_path):
    path = tmp_path / "ref" / "name_cik_cache.csv"
    assert load_name_cache(path) == {}  # absent -> empty
    n1 = write_name_cache(path, [("Apple Inc.", "320193"), ("Sunrise Corp", "111111")])
    assert n1 == 2
    n2 = write_name_cache(path, [("Apple Inc.", "320193")])  # dup, no growth
    assert n2 == 2
    loaded = load_name_cache(path)
    assert loaded["APPLE"] == "0000320193"
    assert loaded["SUNRISE"] == "0000111111"


def test_name_collision_resolved_by_date_window(make_fetcher):
    # "SUNRISE" -> {111111, 222222}. On 2015, only 111111 still bears the name;
    # 222222 had renamed to NEWCO by 2010, so the date singles out 111111.
    index = {"SUNRISE": {"0000111111", "0000222222"}}
    routes = {
        "CIK0000111111.json": {"name": "SUNRISE CORP", "formerNames": []},
        "CIK0000222222.json": {"name": "NEWCO INC", "formerNames": [
            {"name": "Sunrise Corporation",
             "from": "2000-01-01T00:00:00.000Z", "to": "2010-01-01T00:00:00.000Z"}]},
    }
    fetcher = make_fetcher(routes)
    resolved, collisions, unresolved = resolve_names(
        ["Sunrise Corp"], index,
        dates={"Sunrise Corp": "2015-06-01"}, fetcher=fetcher)
    assert resolved == {"Sunrise Corp": "0000111111"}
    assert collisions == []


def test_name_collision_unbroken_when_date_does_not_separate(make_fetcher):
    # On 2005 BOTH bore the name -> the collision stands.
    index = {"SUNRISE": {"0000111111", "0000222222"}}
    routes = {
        "CIK0000111111.json": {"name": "SUNRISE CORP", "formerNames": []},
        "CIK0000222222.json": {"name": "NEWCO INC", "formerNames": [
            {"name": "Sunrise Corporation",
             "from": "2000-01-01T00:00:00.000Z", "to": "2010-01-01T00:00:00.000Z"}]},
    }
    fetcher = make_fetcher(routes)
    resolved, collisions, unresolved = resolve_names(
        ["Sunrise Corp"], index,
        dates={"Sunrise Corp": "2005-06-01"}, fetcher=fetcher)
    assert resolved == {}
    assert collisions == [{"name": "Sunrise Corp",
                           "candidates": ["0000111111", "0000222222"]}]


class _NoFTS:
    """An fts stub that must never be called when the name tier resolves first."""
    def resolve(self, cusip):
        raise AssertionError("fts must not run when the name tier resolves the row")


def test_reconcile_name_tier_resolves_before_fts():
    rows = [{"cik": "", "ticker": "", "cusip6": "", "cusip": "12345678",
             "name": "Widget Inc"}]
    name_index = {"WIDGET": {"0000999999"}}
    issuers, collisions, unresolved = reconcile_identifiers(
        rows, {}, {}, fts=_NoFTS(), name_index=name_index)
    assert len(issuers) == 1
    assert issuers[0].cik == "0000999999"
    assert issuers[0].resolution == "name"
    assert collisions == [] and unresolved == []


def test_reconcile_name_collision_falls_through_to_unresolved():
    rows = [{"cik": "", "ticker": "", "cusip6": "", "cusip": "",
             "name": "Sunrise Corp"}]
    name_index = {"SUNRISE": {"0000111111", "0000222222"}}
    issuers, collisions, unresolved = reconcile_identifiers(
        rows, {}, {}, name_index=name_index)
    assert issuers == []
    assert unresolved == ["Sunrise Corp"]


def test_resolve_member_names_fills_missing_cik():
    members = [
        {"ticker": "AAPL", "company": "Apple Inc.", "cik": "0000320193",
         "first_seen": "", "last_seen": "current"},
        {"ticker": "OLDCO", "company": "Sunrise Corp", "cik": "",
         "first_seen": "1998-01-02", "last_seen": "2012-03-04"},
        {"ticker": "GHOST", "company": "Phantom Industries", "cik": "",
         "first_seen": "", "last_seen": ""},
    ]
    index = {"SUNRISE": {"0000111111"}}  # unique -> no date lookup needed
    out, resolved = resolve_member_names(members, index)
    by_ticker = {m["ticker"]: m for m in out}
    assert by_ticker["OLDCO"]["cik"] == "0000111111"
    assert by_ticker["AAPL"]["cik"] == "0000320193"   # already had one, untouched
    assert by_ticker["GHOST"]["cik"] == ""            # not in index
    assert resolved == {"Sunrise Corp": "0000111111"}


def test_issuers_from_sp500_dual_class_provenance_label(monkeypatch, make_fetcher):
    """Dual-class shares sharing a company name must not bleed the 'name' label.

    FOXA (ticker in SEC map -> ticker-resolved) and FOXB (ticker absent from
    map -> resolved by name tier) both have company='Dual Co'.  Only FOXB must
    carry resolution='name'; FOXA (already ticker-resolved before the name tier
    runs) must carry resolution=''.
    """
    from company_corpus.sources.cik_lookup import parse_cik_lookup

    # Two members with the same company string; FOXA is in the ticker map, FOXB is not.
    fake_members = [
        {"ticker": "FOXA", "company": "Dual Co", "cik": "",
         "first_seen": "2010-01-01", "last_seen": "current"},
        {"ticker": "FOXB", "company": "Dual Co", "cik": "",
         "first_seen": "2010-01-01", "last_seen": "current"},
    ]

    # company_tickers.json: only FOXA present (FOXB absent -> won't ticker-resolve).
    routes = {
        "company_tickers.json": {
            "0": {"cik_str": 1234567, "ticker": "FOXA", "title": "Dual Co"},
        }
    }
    fetcher = make_fetcher(routes)

    # Monkeypatch sp500_membership to return our fake members (no Wikipedia fetch).
    monkeypatch.setattr(
        "company_corpus.indices.sp500_membership",
        lambda fetcher, start=None: (fake_members, []),
    )

    # cik-lookup text: "Dual Co" canonicalizes to "DUAL" -> CIK 0000007777777.
    cik_lookup_text = "DUAL CO:7777777:\n"
    name_index = parse_cik_lookup(cik_lookup_text)

    issuers, _changes, unresolved = issuers_from_sp500(
        fetcher, name_index=name_index)

    assert unresolved == [], f"expected no unresolved, got {unresolved}"
    by_ticker = {i.ticker: i for i in issuers}

    # Both should have *a* CIK resolved.
    assert by_ticker["FOXA"].cik != "", "FOXA must be resolved (ticker map)"
    assert by_ticker["FOXB"].cik != "", "FOXB must be resolved (name tier)"

    # The key assertion: only FOXB carries resolution='name'.
    assert by_ticker["FOXB"].resolution == "name", (
        f"FOXB should be 'name', got {by_ticker['FOXB'].resolution!r}")
    assert by_ticker["FOXA"].resolution == "", (
        f"FOXA was ticker-resolved; must not be labeled 'name', "
        f"got {by_ticker['FOXA'].resolution!r}"
    )
