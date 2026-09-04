import copy
import json
from pathlib import Path

from company_corpus.config import Config
from company_corpus.registers.concepts_no import map_brreg_entry
from company_corpus.registers.identity import resolve_register_specs

# --- Canonical fixtures -------------------------------------------------------
# Two real Brreg entries (Equinor, orgnr 923609016, FY2022), trimmed from the 6
# real entries the live API returns but with the REAL nesting preserved verbatim.
# This exercises the recursive `_leaves` flatten + last-wins dedup, including the
# `driftsresultat.driftsresultat` collision (a scalar named the same as its parent
# object). All values were validated against the live API.
_KONSERN_2022 = {
    "id": 4080685,
    "journalnr": "2023469485",
    "regnskapstype": "KONSERN",
    "regnskapsperiode": {"fraDato": "2022-01-01", "tilDato": "2022-12-31"},
    "valuta": "USD",
    "egenkapitalGjeld": {
        "sumEgenkapitalGjeld": 158021000000.0,
        "egenkapital": {
            "sumEgenkapital": 53989000000.0,
            "opptjentEgenkapital": {},
            "innskuttEgenkapital": {"sumInnskuttEgenkaptial": 53988000000.0},
        },
        "gjeldOversikt": {
            "sumGjeld": 104032000000.0,
            "kortsiktigGjeld": {"sumKortsiktigGjeld": 43806000000.0},
            "langsiktigGjeld": {"sumLangsiktigGjeld": 60226000000.0},
        },
    },
    "eiendeler": {
        "sumVarer": 5205000000.0,
        "sumFordringer": 22452000000.0,
        "sumInvesteringer": 33915000000.0,
        "sumBankinnskuddOgKontanter": 16598000000.0,
        "sumEiendeler": 158021000000.0,
        "omloepsmidler": {"sumOmloepsmidler": 78170000000.0},
        "anleggsmidler": {"sumAnleggsmidler": 79851000000.0},
    },
    "resultatregnskapResultat": {
        "ordinaertResultatFoerSkattekostnad": 78604000000.0,
        "ordinaertResultatSkattekostnad": 49860000000.0,
        "aarsresultat": 28744000000.0,
        "finansresultat": {
            "nettoFinans": -207000000.0,
            "finansinntekt": {},
            "finanskostnad": {
                "annenRentekostnad": 1379000000.0,   # gross "other" interest
                "sumFinanskostnad": 207000000.0,      # NET aggregate (|nettoFinans|)
            },
        },
        "driftsresultat": {
            "driftsresultat": 78811000000.0,           # scalar named like its parent
            "driftsinntekter": {
                "salgsinntekter": 149004000000.0,
                "sumDriftsinntekter": 150806000000.0,
            },
            "driftskostnad": {"sumDriftskostnad": 71995000000.0},
        },
    },
}

_SELSKAP_2022 = {
    "id": 4080683,
    "journalnr": "2023469485",
    "regnskapstype": "SELSKAP",
    "regnskapsperiode": {"fraDato": "2022-01-01", "tilDato": "2022-12-31"},
    "valuta": "USD",
    "egenkapitalGjeld": {
        "sumEgenkapitalGjeld": 159342000000.0,
        "egenkapital": {
            "sumEgenkapital": 50914000000.0,
            "opptjentEgenkapital": {"sumOpptjentEgenkapital": 49772000000.0},
            "innskuttEgenkapital": {"sumInnskuttEgenkaptial": 1142000000.0},
        },
        "gjeldOversikt": {
            "sumGjeld": 108428000000.0,
            "kortsiktigGjeld": {"sumKortsiktigGjeld": 74830000000.0},
            "langsiktigGjeld": {"sumLangsiktigGjeld": 33598000000.0},
        },
    },
    "eiendeler": {
        "sumVarer": 1771000000.0,
        "sumFordringer": 40603000000.0,
        "sumInvesteringer": 30445000000.0,
        "sumBankinnskuddOgKontanter": 10204000000.0,
        "sumEiendeler": 159342000000.0,
        "omloepsmidler": {"sumOmloepsmidler": 83023000000.0},
        "anleggsmidler": {"sumAnleggsmidler": 76319000000.0},
    },
    "resultatregnskapResultat": {
        "ordinaertResultatFoerSkattekostnad": 27478000000.0,
        "ordinaertResultatSkattekostnad": -68000000.0,
        "aarsresultat": 27546000000.0,
        "finansresultat": {
            "nettoFinans": -887000000.0,
            "finansinntekt": {},
            # Only sumFinanskostnad, NO annenRentekostnad -> the F1 case.
            "finanskostnad": {"sumFinanskostnad": 887000000.0},
        },
        "driftsresultat": {
            "driftsresultat": 28365000000.0,
            "driftsinntekter": {
                "salgsinntekter": 68154000000.0,
                "sumDriftsinntekter": 96784000000.0,
            },
            "driftskostnad": {"sumDriftskostnad": 68419000000.0},
        },
    },
}


# --- Mapping (the recursive flatten + last-wins, over the real nesting) --------
def test_map_konsern_real_nested_values():
    m = map_brreg_entry(_KONSERN_2022)
    assert m["period_end"] == "2022-12-31"
    assert m["basis"] == "consolidated"        # KONSERN
    assert m["currency"] == "USD"
    v = m["values"]
    # (a) operating_income resolves to the scalar `driftsresultat`, not the parent
    #     object of the same name (the collision resolves to the numeric leaf).
    assert v["operating_income"]["value"] == 78811000000
    assert v["operating_income"]["tag"] == "driftsresultat"
    # (b) revenue resolves to the *nested* …driftsinntekter.sumDriftsinntekter
    assert v["revenue"]["value"] == 150806000000
    assert v["revenue"]["tag"] == "sumDriftsinntekter"
    # (c) nested sub-object leaves are picked up from their sub-objects
    assert v["assets_current"]["value"] == 78170000000      # omloepsmidler.sumOmloepsmidler
    assert v["assets_current"]["tag"] == "sumOmloepsmidler"
    assert v["equity"]["value"] == 53989000000              # egenkapital.sumEgenkapital
    assert v["equity"]["tag"] == "sumEgenkapital"
    assert v["liabilities"]["value"] == 104032000000        # gjeldOversikt.sumGjeld
    assert v["liabilities"]["tag"] == "sumGjeld"
    # headline + debt components
    assert v["net_income"]["value"] == 28744000000
    assert v["assets"]["value"] == 158021000000
    assert v["short_term_debt"]["value"] == 43806000000
    assert v["long_term_debt"]["value"] == 60226000000
    # F1: gross interest only (annenRentekostnad), not the net sumFinanskostnad
    assert v["interest_expense"]["value"] == 1379000000
    assert v["interest_expense"]["tag"] == "annenRentekostnad"


def test_map_selskap_real_values_and_basis():
    m = map_brreg_entry(_SELSKAP_2022)
    assert m["basis"] == "company"             # SELSKAP
    assert m["currency"] == "USD"
    v = m["values"]
    assert v["revenue"]["value"] == 96784000000
    assert v["net_income"]["value"] == 27546000000
    assert v["assets"]["value"] == 159342000000
    assert v["equity"]["value"] == 50914000000
    assert v["liabilities"]["value"] == 108428000000


def test_f1_interest_expense_drops_net_finanskostnad():
    # SELSKAP has only `sumFinanskostnad` (a net figure) and no `annenRentekostnad`:
    # interest_expense must be ABSENT, never silently mapped to the net figure.
    m = map_brreg_entry(_SELSKAP_2022)
    assert "interest_expense" not in m["values"]


def test_no_period_returns_none():
    assert map_brreg_entry({"regnskapstype": "KONSERN"}) is None


def test_f6_non_iso_tildato_returns_none():
    bad = {**_KONSERN_2022, "regnskapsperiode": {"tilDato": "31.12.2024"}}
    assert map_brreg_entry(bad) is None


# --- Identity resolution ------------------------------------------------------
class _GleifFetcher:
    def __init__(self, country, registered_as):
        self._c, self._r = country, registered_as
    def get_json(self, url, **kw):
        return {"data": {"attributes": {"entity": {
            "legalName": {"name": "ACME NORGE AS"},
            "legalAddress": {"country": self._c}, "registeredAs": self._r}}}}

def test_orgnr_passes_through():
    r = resolve_register_specs([{"orgnr": "923609016"}], fetcher=None)[0]
    assert r["orgnr"] == "923609016" and r["status"] == "ok" and r["country"] == "NO"

def test_lei_resolves_via_gleif_registeredas():
    r = resolve_register_specs([{"lei": "L1"}], fetcher=_GleifFetcher("NO", "NO 923 609 016"))[0]
    assert r["orgnr"] == "923609016" and r["lei"] == "L1" and r["status"] == "ok"

def test_non_norwegian_lei_is_unresolved():
    r = resolve_register_specs([{"lei": "L2"}], fetcher=_GleifFetcher("SE", "5560000000"))[0]
    assert r["orgnr"] is None and r["status"] == "unresolved"

def test_gleif_exception_is_unresolved():
    class _Bad:
        def get_json(self, url, **kw):
            raise OSError("timeout")
    r = resolve_register_specs([{"lei": "L3"}], fetcher=_Bad())[0]
    assert r["orgnr"] is None and r["status"] == "unresolved"


# --- Producer -----------------------------------------------------------------
class _BrregFetcher:
    def __init__(self, entries): self._e = entries
    def get_json(self, url, **kw): return self._e   # the brreg accounts list


def _run(tmp_path, entries, orgnr="923609016", write=True):
    from company_corpus.registers.financials import build_register_financials
    cfg = Config(data_dir=tmp_path)
    rep = build_register_financials([{"orgnr": orgnr}], fetcher=_BrregFetcher(entries),
                                    config=cfg, write=write)
    path = tmp_path / "financials_register" / f"{orgnr}.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return rep, rows


def test_build_register_financials_real_fixture(tmp_path):
    rep, rows = _run(tmp_path, [_KONSERN_2022, _SELSKAP_2022])
    assert rep["with_financials"] == 1 and rep["periods"] == 2
    # consolidated headline (validated against the live API)
    krev = next(r for r in rows if r["kind"] == "reported"
                and r["concept"] == "revenue" and r["basis"] == "consolidated")
    assert krev["value"] == 150806000000 and krev["currency"] == "USD"
    assert krev["entity_id"] == "923609016" and krev["source"] == "brreg"
    # company headline
    srev = next(r for r in rows if r["kind"] == "reported"
                and r["concept"] == "revenue" and r["basis"] == "company")
    assert srev["value"] == 96784000000
    # debt_to_equity = total liabilities / equity (NGAAP gearing), both bases
    kd2e = next(r for r in rows if r["kind"] == "derived"
                and r["concept"] == "debt_to_equity" and r["basis"] == "consolidated")
    assert abs(kd2e["value"] - (104032000000 / 53989000000)) < 1e-6   # ~1.93
    sd2e = next(r for r in rows if r["kind"] == "derived"
                and r["concept"] == "debt_to_equity" and r["basis"] == "company")
    assert abs(sd2e["value"] - (108428000000 / 50914000000)) < 1e-6   # ~2.13
    # C1: NGAAP gearing is total-liabilities-based -> stamped on the leverage rows.
    assert kd2e["leverage_basis"] == "total_liabilities"
    assert sd2e["leverage_basis"] == "total_liabilities"
    # No TTM rows are ever written for register inputs (annual-only -> inert).
    assert not any(r["kind"] == "derived_ttm" for r in rows)


def test_f1_interest_coverage_only_when_gross_interest_present(tmp_path):
    _, rows = _run(tmp_path, [_KONSERN_2022, _SELSKAP_2022])
    bases = {r["basis"] for r in rows
             if r["kind"] == "derived" and r["concept"] == "interest_coverage"}
    # consolidated has annenRentekostnad -> coverage; company has only the net
    # figure (sumFinanskostnad) -> interest_coverage is not emitted.
    assert bases == {"consolidated"}


def test_f4_dedupe_keeps_highest_submission_id(tmp_path):
    # Same (period, type) twice with different submission ids: only the higher-id
    # (latest resubmission) one is emitted, no double-count. The higher-id entry is
    # fed FIRST to prove selection is id-based, not merely last-seen.
    new = copy.deepcopy(_KONSERN_2022); new["id"] = 2          # real 150_806_000_000
    old = copy.deepcopy(_KONSERN_2022); old["id"] = 1
    old["resultatregnskapResultat"]["driftsresultat"]["driftsinntekter"]["sumDriftsinntekter"] = 99000000.0
    rep, rows = _run(tmp_path, [new, old])
    assert rep["periods"] == 1
    revs = [r["value"] for r in rows if r["kind"] == "reported" and r["concept"] == "revenue"]
    assert revs == [150806000000]                              # only the higher-id survived


def test_f6_bad_date_skips_one_entry_not_batch(tmp_path):
    # A non-ISO tilDato skips THAT entry; the rest of the batch still produces rows.
    bad = {**_SELSKAP_2022, "regnskapsperiode": {"tilDato": "31.12.2024"}}
    rep, rows = _run(tmp_path, [bad, _KONSERN_2022])
    assert rep["with_financials"] == 1 and rep["periods"] == 1
    assert any(r["concept"] == "revenue" and r["value"] == 150806000000 for r in rows)


def test_dry_run_writes_nothing(tmp_path):
    rep, _ = _run(tmp_path, [_KONSERN_2022], write=False)
    assert rep["coverage_path"] is None and not (tmp_path / "financials_register").exists()


from company_corpus import cli

def test_register_financials_cli(monkeypatch, tmp_path):
    captured = {}
    def fake(specs, *, fetcher, config, write):
        captured.update(specs=specs, write=write)
        return {"entities": 1, "with_financials": 1, "periods": 3, "coverage_path": None, "paths": []}
    monkeypatch.setattr(cli, "build_register_financials", fake)
    args = cli.build_parser().parse_args(["register-financials", "--orgnrs", "1,2", "--write"])
    assert args.func(args) == 0
    assert captured["specs"] == [{"orgnr": "1"}, {"orgnr": "2"}] and captured["write"] is True


# --- Review fixes I1/I2/I3 ----------------------------------------------------
# Real Brreg entries captured live for small AS filers that report NO non-current
# liabilities: `langsiktigGjeld` is an EMPTY dict, so the `sumLangsiktigGjeld` leaf is
# ABSENT — exactly the case I2 must synthesize so gearing still computes.
_NO_LTDEBT_POS = {  # orgnr 936133711, SELSKAP 2025, POSITIVE equity 13_858_635
    "id": 6590397,
    "regnskapstype": "SELSKAP",
    "regnskapsperiode": {"fraDato": "2025-08-22", "tilDato": "2025-12-31"},
    "valuta": "NOK",
    "egenkapitalGjeld": {
        "sumEgenkapitalGjeld": 13865135.0,
        "egenkapital": {
            "sumEgenkapital": 13858635.0,
            "opptjentEgenkapital": {"sumOpptjentEgenkapital": 13400197.0},
            "innskuttEgenkapital": {"sumInnskuttEgenkaptial": 458439.0},
        },
        "gjeldOversikt": {
            "sumGjeld": 6500.0,
            "kortsiktigGjeld": {"sumKortsiktigGjeld": 6500.0},
            "langsiktigGjeld": {},                 # <- no sumLangsiktigGjeld leaf
        },
    },
    "eiendeler": {
        "sumFordringer": 0.0,
        "sumInvesteringer": 10874020.0,
        "sumBankinnskuddOgKontanter": 201489.0,
        "sumEiendeler": 13865135.0,
        "omloepsmidler": {"sumOmloepsmidler": 11075509.0},
        "anleggsmidler": {"sumAnleggsmidler": 2789627.0},
    },
    "resultatregnskapResultat": {
        "ordinaertResultatFoerSkattekostnad": 176255.0,
        "aarsresultat": 176255.0,
        "finansresultat": {
            "nettoFinans": 184005.0,
            "finansinntekt": {"sumFinansinntekter": 184005.0},
            "finanskostnad": {"sumFinanskostnad": 0.0},
        },
        "driftsresultat": {
            "driftsresultat": -7750.0,
            "driftsinntekter": {"sumDriftsinntekter": 0.0},
            "driftskostnad": {"sumDriftskostnad": 7750.0},
        },
    },
}

_NO_LTDEBT_NEG = {  # orgnr 935211026, SELSKAP 2025, NEGATIVE equity -168_967
    "id": 6409129,
    "regnskapstype": "SELSKAP",
    "regnskapsperiode": {"fraDato": "2025-03-04", "tilDato": "2025-12-31"},
    "valuta": "NOK",
    "egenkapitalGjeld": {
        "sumEgenkapitalGjeld": 124869.0,
        "egenkapital": {
            "sumEgenkapital": -168967.0,
            "opptjentEgenkapital": {"sumOpptjentEgenkapital": -198967.0},
            "innskuttEgenkapital": {"sumInnskuttEgenkaptial": 30000.0},
        },
        "gjeldOversikt": {
            "sumGjeld": 293836.0,
            "kortsiktigGjeld": {"sumKortsiktigGjeld": 293836.0},
            "langsiktigGjeld": {},                 # <- no sumLangsiktigGjeld leaf
        },
    },
    "eiendeler": {
        "sumFordringer": 100000.0,
        "sumInvesteringer": 0.0,
        "sumBankinnskuddOgKontanter": 24869.0,
        "sumEiendeler": 124869.0,
        "omloepsmidler": {"sumOmloepsmidler": 124869.0},
        "anleggsmidler": {"sumAnleggsmidler": 0.0},
    },
    "resultatregnskapResultat": {
        "ordinaertResultatFoerSkattekostnad": -198967.0,
        "aarsresultat": -198967.0,
        "finansresultat": {
            "nettoFinans": 0.0,
            "finansinntekt": {"sumFinansinntekter": 0.0},
            "finanskostnad": {"sumFinanskostnad": 0.0},
        },
        "driftsresultat": {
            "driftsresultat": -198967.0,
            "driftsinntekter": {"sumDriftsinntekter": 100000.0},
            "driftskostnad": {"loennskostnad": 99180.0, "sumDriftskostnad": 298967.0},
        },
    },
}


class _MultiBrregFetcher:
    """Brreg accounts fetcher that dispatches by the orgnr embedded in the URL."""
    def __init__(self, by_orgnr): self._by = by_orgnr
    def get_json(self, url, **kw):
        orgnr = url.rstrip("/").rsplit("/", 1)[-1]
        return self._by.get(orgnr, [])


# --- I1: tangible_book_value is structurally unprovable -> never emitted ------
def test_i1_tangible_book_value_suppressed(tmp_path):
    # Equinor is a complete filer for which the engine WOULD compute tangible_book_value
    # (it collapses to equity); the producer must suppress it (and its per-share form).
    _, rows = _run(tmp_path, [_KONSERN_2022, _SELSKAP_2022])
    assert rows  # sanity: rows were emitted
    assert not any(r["concept"] == "tangible_book_value" for r in rows)
    assert not any(r["concept"] == "tangible_book_value_per_share" for r in rows)
    # control: a normal derived metric is still present
    assert any(r["kind"] == "derived" and r["concept"] == "debt_to_equity" for r in rows)


# --- I2: synthesize long_term_debt so gearing computes for no-LT-debt filers --
def test_i2_synthesizes_long_term_debt_when_leaf_absent():
    m = map_brreg_entry(_NO_LTDEBT_POS)
    ltd = m["values"]["long_term_debt"]
    assert ltd["value"] == 0                        # 6500 (sumGjeld) - 6500 (sumKortsiktigGjeld)
    assert "derived" in ltd["tag"]                  # marked as synthesized, not a real leaf


def test_i2a_gearing_for_no_lt_debt_positive_equity(tmp_path):
    _, rows = _run(tmp_path, [_NO_LTDEBT_POS], orgnr="936133711")
    td = next(r for r in rows if r["kind"] == "derived" and r["concept"] == "total_debt")
    assert td["value"] == 6500                       # = sumGjeld, via synth LTD(0)+short(6500)
    # positive equity -> debt_to_equity IS emitted (engine's div_pos passes)
    assert any(r["kind"] == "derived" and r["concept"] == "debt_to_equity" for r in rows)


def test_i2b_gearing_for_no_lt_debt_negative_equity(tmp_path):
    _, rows = _run(tmp_path, [_NO_LTDEBT_NEG], orgnr="935211026")
    td = next(r for r in rows if r["kind"] == "derived" and r["concept"] == "total_debt")
    assert td["value"] == 293836
    # debt_to_assets present; debt_to_equity may be suppressed (non-positive equity) -> not required
    assert any(r["kind"] == "derived" and r["concept"] == "debt_to_assets" for r in rows)


def test_i2_noop_when_sum_langsiktig_present():
    # Equinor KONSERN has a REAL sumLangsiktigGjeld leaf -> synthesis is skipped and
    # long_term_debt keeps the real tag (confirms the no-op path).
    m = map_brreg_entry(_KONSERN_2022)
    ltd = m["values"]["long_term_debt"]
    assert ltd["value"] == 60226000000
    assert ltd["tag"] == "sumLangsiktigGjeld"
    assert "derived" not in ltd["tag"]


# --- I3: one malformed record must not abort the batch (nor lose coverage) ----
def test_i3_malformed_record_does_not_abort_batch(tmp_path):
    from company_corpus.registers.financials import build_register_financials
    # `regnskapsperiode` as a string -> AttributeError in dedupe/map for the first orgnr;
    # the second orgnr is well-formed and must still be processed.
    bad = {**_KONSERN_2022, "regnskapsperiode": "not-a-dict"}
    cfg = Config(data_dir=tmp_path)
    fetcher = _MultiBrregFetcher({"100000000": [bad], "923609016": [_KONSERN_2022]})
    rep = build_register_financials(
        [{"orgnr": "100000000"}, {"orgnr": "923609016"}],
        fetcher=fetcher, config=cfg, write=True)
    assert rep["errors"] == 1                         # counted separately, not as no_financials
    assert rep["no_financials"] == 0
    assert rep["with_financials"] == 1
    cov_path = tmp_path / "reports" / "register_coverage_brreg.jsonl"
    assert cov_path.exists()                          # coverage write still ran post-loop
    cov = {c["orgnr"]: c for c in (json.loads(x) for x in cov_path.read_text().splitlines())}
    assert cov["100000000"]["status"] == "error" and "error" in cov["100000000"]
    assert cov["923609016"]["status"] == "ok"
    assert (tmp_path / "financials_register" / "923609016.jsonl").exists()


# --- R-I1: NO balance gate regression tests -----------------------------------

def test_ri1_map_balanced_entry_has_6_key_shape():
    """map_brreg_entry returns the 6-key shape (unbalanced=False) for a balanced entry."""
    m = map_brreg_entry(_KONSERN_2022)
    assert "unbalanced" in m
    assert "suppressed" in m
    assert m["unbalanced"] is False
    assert m["suppressed"] == []


def test_ri1_unbalanced_entry_returns_empty_values():
    """Synthetic entry with assets != equity+liabilities beyond tol → unbalanced=True,
    values={}.  The KONSERN_2022 fixture is balanced; we corrupt it by halving assets."""
    import copy
    bad = copy.deepcopy(_KONSERN_2022)
    # Set sumEiendeler to half the true value so assets << equity+liabilities
    bad["eiendeler"]["sumEiendeler"] = 1_000.0   # was 158_021_000_000
    m = map_brreg_entry(bad)
    assert m is not None                          # still returns a dict (not None)
    assert m["unbalanced"] is True
    assert m["values"] == {}
    assert any("assets" in str(s) for s in m["suppressed"])


def test_ri1_producer_skips_unbalanced_entries(tmp_path):
    """build_register_financials: an unbalanced entry is counted in out['unbalanced'],
    produces no rows, and does NOT write a financials JSONL for that entity."""
    import copy
    from company_corpus.registers.financials import build_register_financials

    bad = copy.deepcopy(_KONSERN_2022)
    bad["eiendeler"]["sumEiendeler"] = 1_000.0   # corrupt assets to force unbalanced

    cfg = Config(data_dir=tmp_path)
    fetcher = _MultiBrregFetcher({"000000001": [bad], "923609016": [_KONSERN_2022]})
    rep = build_register_financials(
        [{"orgnr": "000000001"}, {"orgnr": "923609016"}],
        fetcher=fetcher, config=cfg, write=True,
    )
    assert rep["unbalanced"] == 1, f"expected unbalanced=1, got {rep}"
    assert rep["with_financials"] == 1
    assert not (tmp_path / "financials_register" / "000000001.jsonl").exists()
    assert (tmp_path / "financials_register" / "923609016.jsonl").exists()


def test_ri1_real_equinor_fixture_still_balanced(tmp_path):
    """R-I1 non-regression: the real Equinor KONSERN+SELSKAP fixture is balanced;
    rows are still emitted and no entity is counted as unbalanced."""
    _, rows = _run(tmp_path, [_KONSERN_2022, _SELSKAP_2022])
    assert rows, "must still emit rows for the balanced real fixture"
    assert any(r["kind"] == "reported" and r["concept"] == "revenue" for r in rows)


def test_i3b_heterogeneous_ids_do_not_crash_dedup(tmp_path):
    # Two entries for the SAME (period, type) with heterogeneous ids (str vs int): the old
    # `e_id >= cur_id` raised TypeError; the type-safe guard keeps last-seen instead.
    a = copy.deepcopy(_KONSERN_2022); a["id"] = "x"   # non-int id
    b = copy.deepcopy(_KONSERN_2022); b["id"] = 5
    rep, rows = _run(tmp_path, [a, b])
    assert rep["errors"] == 0 and rep["with_financials"] == 1 and rep["periods"] == 1
    assert any(r["concept"] == "revenue" and r["value"] == 150806000000 for r in rows)


# --- R-I3: per-source coverage file, no clobber --------------------------------

def test_ri3_two_registers_same_data_dir_no_clobber(tmp_path):
    """R-I3 regression: running two different registers into the same data_dir must
    produce TWO separate coverage files (one per source) — neither clobbers the other.

    Uses NO (build_register_financials → register_coverage_brreg.jsonl) and
    DK-GAAP FSA (build_dk_financials_from_files → register_coverage_erst.jsonl).
    """
    from company_corpus.registers.financials import (
        build_register_financials,
        build_dk_financials_from_files,
    )

    cfg = Config(data_dir=tmp_path)

    # --- Run NO producer ---
    build_register_financials(
        [{"orgnr": "923609016"}],
        fetcher=_BrregFetcher([_KONSERN_2022]),
        config=cfg, write=True,
    )

    # --- Run DK producer on a real FSA fixture ---
    dk_fixture = Path(__file__).parent / "fixtures" / "dk" / "dk_30830725_microB_2025.xml"
    build_dk_financials_from_files(
        [str(dk_fixture)],
        config=cfg, write=True,
    )

    reports = tmp_path / "reports"
    brreg_cov = reports / "register_coverage_brreg.jsonl"
    erst_cov  = reports / "register_coverage_erst.jsonl"

    assert brreg_cov.exists(), "register_coverage_brreg.jsonl must exist after NO run"
    assert erst_cov.exists(),  "register_coverage_erst.jsonl must exist after DK run"

    # Each file must contain only its own register's entries
    brreg_rows = [json.loads(x) for x in brreg_cov.read_text().splitlines() if x.strip()]
    erst_rows  = [json.loads(x) for x in erst_cov.read_text().splitlines() if x.strip()]
    assert any("orgnr" in r for r in brreg_rows),  "brreg coverage must have orgnr entries"
    assert any("cvr"   in r for r in erst_rows),   "erst coverage must have cvr entries"
    # Crucially: no cross-contamination
    assert not any("cvr"   in r for r in brreg_rows), "brreg file must not contain DK entries"
    assert not any("orgnr" in r for r in erst_rows),  "erst file must not contain NO entries"
