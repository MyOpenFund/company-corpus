"""Tests for company_corpus.registers.sk_registeruz — Task 1.

Network-free: all assertions run against committed fixtures.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from company_corpus.registers.identity import _norm_ico, resolve_register_specs

FIXTURES = Path(__file__).parent / "fixtures" / "sk"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# parse_vykaz — positional extractor
# ---------------------------------------------------------------------------

def test_parse_vykaz_metadata():
    """Top-level metadata fields are extracted correctly."""
    from company_corpus.registers.sk_registeruz import parse_vykaz

    v = _load("sk_36319007_POD.json")
    s = _load("sk_sablona_699.json")
    p = parse_vykaz(v, s)

    assert p["idSablony"] == 699
    assert p["ico"] == "36319007"
    assert p["pristupnostDat"] == "Verejné"


def test_parse_vykaz_assets_netto_col2():
    """Table 0, cisloRiadku=1 (SPOLU MAJETOK) — netto-current = column 2 → 1 051 307."""
    from company_corpus.registers.sk_registeruz import parse_vykaz

    v = _load("sk_36319007_POD.json")
    s = _load("sk_sablona_699.json")
    p = parse_vykaz(v, s)

    # Table 0 has ncols=4 from sablona.  cisloRiadku=1 is the first row
    # (row_arr_idx=0).  data[0*4 + 2] = "1051307" → 1 051 307.0
    assert p["cells"][(0, 1)][2] == 1051307.0


def test_parse_vykaz_equity_col0():
    """Table 1, cisloRiadku=80 (Vlastné imanie / Equity) — col 0 → 262 763."""
    from company_corpus.registers.sk_registeruz import parse_vykaz

    v = _load("sk_36319007_POD.json")
    s = _load("sk_sablona_699.json")
    p = parse_vykaz(v, s)

    # Table 1 has ncols=2; row_arr_idx of cisloRiadku=80 = 1 (after 79).
    # data[1*2 + 0] = "262763" → 262 763.0
    assert p["cells"][(1, 80)][0] == 262763.0


def test_parse_vykaz_lt_bank_loans_col0():
    """Table 1, cisloRiadku=121 (Dlhodobé bankové úvery) — col 0 → 150 722."""
    from company_corpus.registers.sk_registeruz import parse_vykaz

    v = _load("sk_36319007_POD.json")
    s = _load("sk_sablona_699.json")
    p = parse_vykaz(v, s)

    # cisloRiadku=121 is at row_arr_idx=42 (79→0, 80→1, ..., 121→42).
    # data[42*2 + 0] = "150722" → 150 722.0
    assert p["cells"][(1, 121)][0] == 150722.0


def test_parse_vykaz_empty_cells_are_none():
    """Empty strings in the data array become None, not 0."""
    from company_corpus.registers.sk_registeruz import parse_vykaz

    v = _load("sk_36319007_POD.json")
    s = _load("sk_sablona_699.json")
    p = parse_vykaz(v, s)

    # Table 0, cisloRiadku=5 (Softvér) is all-empty in this fixture.
    assert p["cells"][(0, 5)] == [None, None, None, None]


def test_parse_vykaz_cells_keyed_by_table_and_cislo():
    """Cells dict is keyed by (table_idx, cisloRiadku) tuples."""
    from company_corpus.registers.sk_registeruz import parse_vykaz

    v = _load("sk_36319007_POD.json")
    s = _load("sk_sablona_699.json")
    p = parse_vykaz(v, s)

    assert isinstance(p["cells"], dict)
    # Spot-check a key exists and is the right shape
    row = p["cells"][(0, 1)]
    assert len(row) == 4  # ncols=4 for table 0


# ---------------------------------------------------------------------------
# API client functions — network-free, using a mock fetcher
# ---------------------------------------------------------------------------

def _make_fetcher(responses: dict) -> MagicMock:
    """Build a mock fetcher whose get_json returns pre-canned payloads by URL prefix."""
    fetcher = MagicMock()
    def get_json(url, *, params=None, **_):
        for prefix, payload in responses.items():
            if url.startswith(prefix):
                return payload
        raise ValueError(f"Unexpected URL: {url}")
    fetcher.get_json.side_effect = get_json
    return fetcher


def test_fetch_vykaz():
    """fetch_vykaz returns parsed JSON from the /uctovny-vykaz endpoint."""
    from company_corpus.registers.sk_registeruz import fetch_vykaz

    payload = {"id": 9000014, "idSablony": 699}
    fetcher = _make_fetcher({"https://www.registeruz.sk/cruz-public/api/uctovny-vykaz": payload})
    result = fetch_vykaz(9000014, fetcher=fetcher)
    assert result == payload


def test_fetch_sablona():
    """fetch_sablona returns parsed JSON from the /sablona endpoint."""
    from company_corpus.registers.sk_registeruz import fetch_sablona

    payload = {"id": 699, "nazov": "Úč POD"}
    fetcher = _make_fetcher({"https://www.registeruz.sk/cruz-public/api/sablona": payload})
    result = fetch_sablona(699, fetcher=fetcher)
    assert result == payload


def test_fetch_entity():
    """fetch_entity returns parsed JSON from the /uctovna-jednotka endpoint."""
    from company_corpus.registers.sk_registeruz import fetch_entity

    payload = {"id": 123, "ico": "36319007"}
    fetcher = _make_fetcher({"https://www.registeruz.sk/cruz-public/api/uctovna-jednotka": payload})
    result = fetch_entity(123, fetcher=fetcher)
    assert result == payload


def test_fetch_zavierka():
    """fetch_zavierka returns parsed JSON from the /uctovna-zavierka endpoint."""
    from company_corpus.registers.sk_registeruz import fetch_zavierka

    payload = {"id": 456, "idUctovnejJednotky": 123}
    fetcher = _make_fetcher({"https://www.registeruz.sk/cruz-public/api/uctovna-zavierka": payload})
    result = fetch_zavierka(456, fetcher=fetcher)
    assert result == payload


def test_fetch_vykaz_returns_none_on_error():
    """fetch_vykaz is batch-safe and returns None on network error."""
    from company_corpus.registers.sk_registeruz import fetch_vykaz

    fetcher = MagicMock()
    fetcher.get_json.side_effect = OSError("network down")
    assert fetch_vykaz(9000014, fetcher=fetcher) is None


def test_iter_entity_ids_paginates():
    """iter_entity_ids paginates until the returned list is empty."""
    from company_corpus.registers.sk_registeruz import iter_entity_ids

    page1 = {"pokracovat": 1001, "id": list(range(1, 1001))}
    page2 = {"pokracovat": 2001, "id": list(range(1001, 1101))}
    page3 = {"id": []}   # signals stop

    call_count = 0
    def get_json(url, *, params=None, **_):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return page1
        elif call_count == 2:
            return page2
        else:
            return page3

    fetcher = MagicMock()
    fetcher.get_json.side_effect = get_json

    ids = list(iter_entity_ids(fetcher=fetcher))
    assert ids[:5] == [1, 2, 3, 4, 5]
    assert len(ids) == 1100  # 1000 + 100


def test_iter_entity_ids_stops_on_error():
    """iter_entity_ids stops gracefully if the API raises."""
    from company_corpus.registers.sk_registeruz import iter_entity_ids

    fetcher = MagicMock()
    fetcher.get_json.side_effect = OSError("WAF block")

    ids = list(iter_entity_ids(fetcher=fetcher))
    assert ids == []


# ---------------------------------------------------------------------------
# Task 2 — map_sk_vykaz: concept pack + NO-FALSE-DATA gate
# ---------------------------------------------------------------------------

def _suppressed_keys(mapped: dict) -> set:
    return {k for k, _ in mapped["suppressed"]}


def _synth_pod(assets, equity, liab, accruals, *, sablony=699, pristupnost="Verejné"):
    """Minimal POD-shaped (vykaz, sablona) that yields the four gate cells.

    Table 0 (assets) has 4 data columns → assets read from col 2.  Table 1
    carries equity (r80), liabilities (r101) and accruals (r141) at col 0.
    Values pass as ints/None; None → empty-string cell.
    """
    sablona = {"id": sablony, "tabulky": [
        {"pocetDatovychStlpcov": 4, "riadky": [{"cisloRiadku": 1}]},
        {"pocetDatovychStlpcov": 2, "riadky": [
            {"cisloRiadku": 80}, {"cisloRiadku": 101}, {"cisloRiadku": 141}]},
    ]}
    def c(x):
        return "" if x is None else str(x)
    vykaz = {
        "idSablony": sablony,
        "pristupnostDat": pristupnost,
        "obsah": {
            "titulnaStrana": {"ico": "99999999"},
            "tabulky": [
                {"data": ["", "", c(assets), ""]},                         # t0 r1 col2 = assets
                {"data": [c(equity), "", c(liab), "", c(accruals), ""]},    # t1 r80/r101/r141 col0
            ],
        },
    }
    return vykaz, sablona


def _synth_meta(sablony, pristupnost):
    """A metadata-only (vykaz, sablona) with no positional tables."""
    vykaz = {"idSablony": sablony, "pristupnostDat": pristupnost,
             "obsah": {"titulnaStrana": {"ico": "99999999"}, "tabulky": []}}
    return vykaz, {"id": sablony, "tabulky": []}


def test_map_pod_values_shape_and_balance():
    """sk_36319007 POD: balanced (gate uses accruals) → curated values to the cent."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    m = map_sk_vykaz(_load("sk_36319007_POD.json"), _load("sk_sablona_699.json"))

    assert m["basis"] == "company"
    assert m["currency"] == "EUR"
    assert m["unbalanced"] is False

    v = m["values"]
    assert v["assets"]["value"] == 1051307.0
    assert v["equity"]["value"] == 262763.0
    assert v["liabilities"]["value"] == 737181.0
    assert v["net_income"]["value"] == 30719.0
    assert v["operating_income"]["value"] == 58411.0
    assert v["pretax_income"]["value"] == 34838.0
    # every emitted value is EUR and carries an sk:r<cislo> provenance tag
    assert v["assets"]["unit"] == "EUR"
    assert v["equity"]["tag"] == "sk:r80"
    assert v["net_income"]["tag"] == "sk:r61"


def test_map_pod_gate_uses_accruals():
    """POD balance is assets == equity + liabilities + accruals (51 363).

    Without the accruals term equity+liabilities = 999 944 ≠ 1 051 307, so a
    balanced result *proves* the POD-only accruals term is in the gate.
    """
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    m = map_sk_vykaz(_load("sk_36319007_POD.json"), _load("sk_sablona_699.json"))
    v = m["values"]
    assert m["unbalanced"] is False
    # equity + liabilities alone does NOT reach assets — accruals (51 363) close it.
    assert v["equity"]["value"] + v["liabilities"]["value"] == 999944.0
    assert v["equity"]["value"] + v["liabilities"]["value"] + 51363.0 == v["assets"]["value"]


def test_map_pod_revenue_is_operating_revenue_total_not_net_turnover():
    """revenue = operating_revenue_total (r2 = 2 005 372), NEVER net_turnover (r1 = 1 950 307)."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    m = map_sk_vykaz(_load("sk_36319007_POD.json"), _load("sk_sablona_699.json"))
    v = m["values"]
    assert v["revenue"]["value"] == 2005372.0        # operating_revenue_total (r2)
    assert v["revenue"]["value"] != 1950307.0        # NOT net_turnover (r1)
    assert v["revenue"]["tag"] == "sk:r2"
    # the net_turnover trap is recorded as a suppression (auditable no-false-data)
    assert "net_turnover" in _suppressed_keys(m)


def test_map_pod_emits_borrowings_debt_block():
    """Bank-loan lines present + nonzero → borrowings LT/ST emitted; interest positive as-is."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    m = map_sk_vykaz(_load("sk_36319007_POD.json"), _load("sk_sablona_699.json"))
    v = m["values"]
    assert v["long_term_debt"]["value"] == 150722.0
    assert v["long_term_debt"]["tag"] == "sk:r121"
    assert v["short_term_debt"]["value"] == 155854.0
    assert v["short_term_debt"]["tag"] == "sk:r139"
    assert v["interest_expense"]["value"] == 21002.0   # positive as-is, no abs()


def test_map_pod_nodebt_suppresses_debt_block():
    """sk_50296353: zero bank loans → debt block suppressed, equity/assets still emitted, gate holds."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    m = map_sk_vykaz(_load("sk_50296353_POD_nodebt.json"), _load("sk_sablona_699.json"))
    v = m["values"]
    assert m["unbalanced"] is False
    assert "long_term_debt" not in v
    assert "short_term_debt" not in v
    assert {"long_term_debt", "short_term_debt"} <= _suppressed_keys(m)
    assert v["equity"]["value"] == 8627.0
    assert v["assets"]["value"] == 10152.0
    # gate holds with no accruals term: 8 627 + 1 525 == 10 152
    assert v["equity"]["value"] + v["liabilities"]["value"] == 10152.0


def test_map_muj_map_and_gate_no_accruals_term():
    """sk_54953006 MUJ (687): MUJ map; LT bank loan 16 303; gate assets == equity + liabilities."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    m = map_sk_vykaz(_load("sk_54953006_MUJ.json"), _load("sk_sablona_687.json"))
    v = m["values"]
    assert m["unbalanced"] is False
    assert v["assets"]["value"] == 88449.0
    assert v["equity"]["value"] == 6240.0
    assert v["liabilities"]["value"] == 82209.0
    assert v["revenue"]["value"] == 14408.0
    assert v["long_term_debt"]["value"] == 16303.0
    assert v["long_term_debt"]["tag"] == "sk:r37"
    assert "short_term_debt" not in v            # absent bank-loan line → suppressed
    assert v["interest_expense"]["value"] == 74.0
    # MUJ gate has NO accruals term: assets == equity + liabilities exactly
    assert v["equity"]["value"] + v["liabilities"]["value"] == v["assets"]["value"]


def test_map_period_end_from_titulna_strana_month_end():
    """period_end derives from titulnaStrana.obdobieDo (2023-12 → month-end 2023-12-31)."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    m = map_sk_vykaz(_load("sk_36319007_POD.json"), _load("sk_sablona_699.json"))
    assert m["period_end"] == "2023-12-31"


def test_map_synthetic_unbalanced_emits_no_values():
    """assets ≠ equity + liabilities + accruals beyond tol → unbalanced, empty values."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    vykaz, sablona = _synth_pod(assets=1000, equity=100, liab=200, accruals=50)
    m = map_sk_vykaz(vykaz, sablona)
    assert m["unbalanced"] is True
    assert m["values"] == {}
    assert "__all__" in _suppressed_keys(m)


def test_map_synthetic_balanced_within_tol():
    """A synthetic POD that balances (incl. accruals) is not flagged unbalanced."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    vykaz, sablona = _synth_pod(assets=1000, equity=600, liab=350, accruals=50)
    m = map_sk_vykaz(vykaz, sablona)
    assert m["unbalanced"] is False
    assert m["values"]["assets"]["value"] == 1000.0


def test_map_non_public_is_no_financials():
    """pristupnostDat != 'Verejné' → no financials (empty values, recorded reason)."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    vykaz, sablona = _synth_meta(699, "Neverejné")
    m = map_sk_vykaz(vykaz, sablona)
    assert m["values"] == {}
    assert m["unbalanced"] is False
    assert "__all__" in _suppressed_keys(m)


def test_map_ifrs_template_is_no_financials():
    """idSablony=695 (IFRS/other) is not a mapped template → no financials."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    vykaz, sablona = _synth_meta(695, "Verejné")
    m = map_sk_vykaz(vykaz, sablona)
    assert m["values"] == {}
    assert m["unbalanced"] is False
    assert "__all__" in _suppressed_keys(m)


# ---------------------------------------------------------------------------
# Review fixes — Fix 1: template-match guard (no-false-data on --sk-file path)
# ---------------------------------------------------------------------------

def test_map_sablona_vykaz_id_mismatch_is_no_financials():
    """MUJ vykaz (idSablony=687) + POD sablona (id=699): mismatch → no-financials.

    This is the false-data vector on the manual --sk-file path: supplying the
    wrong sablona causes the positional extractor to index with wrong row-order
    and ncols, gate anchors resolve to None (gate skipped) and up to 6
    misaligned values are emitted with unbalanced=False.  The guard must catch
    this *before* any values are produced.
    """
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    m = map_sk_vykaz(_load("sk_54953006_MUJ.json"), _load("sk_sablona_699.json"))
    assert m["values"] == {}, "mismatch must suppress all values (not emit misaligned numbers)"
    assert m["unbalanced"] is False
    reason_map = dict(m["suppressed"])
    assert "__all__" in reason_map, "suppressed must carry an __all__ reason"
    assert "mismatch" in reason_map["__all__"], (
        f"reason must mention 'mismatch'; got: {reason_map['__all__']!r}"
    )


# ---------------------------------------------------------------------------
# Review fixes — Fix 2: parse_vykaz robust to malformed records (no KeyError)
# ---------------------------------------------------------------------------

def test_parse_vykaz_malformed_no_titulna_strana_no_raise():
    """Malformed vykaz (no titulnaStrana, 0 tables) → parse_vykaz returns cells=={} without raising."""
    from company_corpus.registers.sk_registeruz import parse_vykaz

    malformed = {
        "idSablony": 716,
        "pristupnostDat": "Verejne",
        "obsah": {"tabulky": []},   # no titulnaStrana key
    }
    sablona = {"tabulky": []}
    p = parse_vykaz(malformed, sablona)   # must not raise
    assert p["cells"] == {}
    assert p["ico"] is None


def test_map_malformed_vykaz_is_no_financials():
    """Malformed vykaz (no titulnaStrana, idSablony=716) → no-financials, not an error/exception."""
    from company_corpus.registers.concepts_sk import map_sk_vykaz

    malformed = {
        "idSablony": 716,
        "pristupnostDat": "Verejne",
        "obsah": {"tabulky": []},
    }
    sablona = {"id": 716, "tabulky": []}
    m = map_sk_vykaz(malformed, sablona)   # must not raise
    assert m["values"] == {}
    assert m["unbalanced"] is False
    assert "__all__" in _suppressed_keys(m)


# ---------------------------------------------------------------------------
# Task 3 — SK identity (IČO / LEI->GLEIF registeredAs)
# ---------------------------------------------------------------------------


class _GleifFetcherSK:
    """Stub GLEIF fetcher returning a fixed country + registeredAs."""

    def __init__(self, country, registered_as):
        self._c, self._r = country, registered_as

    def get_json(self, url, **kw):
        return {"data": {"attributes": {"entity": {
            "legalName": {"name": "ACME SK S.R.O."},
            "legalAddress": {"country": self._c},
            "registeredAs": self._r,
        }}}}


def test_norm_ico_strips_and_pads():
    """_norm_ico strips non-digits and left-pads to 8 digits."""
    assert _norm_ico(" 31322832 ") == "31322832"
    assert _norm_ico("31322832") == "31322832"
    assert _norm_ico("SK31322832") == "31322832"


def test_sk_lei_resolves_via_gleif_registeredas():
    """A LEI for an SK entity resolves via GLEIF registeredAs -> ico."""
    r = resolve_register_specs(
        [{"lei": "L_SK1"}],
        fetcher=_GleifFetcherSK("SK", "31322832"),
    )[0]
    assert r["ico"] == "31322832"
    assert r["lei"] == "L_SK1"
    assert r["status"] == "ok"
    assert r["country"] == "SK"


def test_non_sk_lei_is_unresolved():
    """A LEI whose GLEIF country != SK must not produce an ico (no-guess)."""
    r = resolve_register_specs(
        [{"lei": "L_CZ1"}],
        fetcher=_GleifFetcherSK("CZ", "27082440"),
    )[0]
    assert r.get("ico") is None
    assert r["status"] == "unresolved"


# ---------------------------------------------------------------------------
# Task 4 — producer (build_sk_financials_from_files) + CLI
# ---------------------------------------------------------------------------

def test_build_sk_financials_from_files_writes_jsonl(tmp_path):
    """build_sk_financials_from_files writes data/financials_register/36319007.jsonl."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_sk_financials_from_files

    cfg = Config(data_dir=tmp_path)
    out = build_sk_financials_from_files(
        FIXTURES / "sk_36319007_POD.json",
        FIXTURES / "sk_sablona_699.json",
        config=cfg,
        write=True,
    )

    assert out["entities"] == 1
    assert out["with_financials"] == 1
    assert out["no_financials"] == 0
    assert out["unbalanced"] == 0
    assert out["errors"] == 0

    out_file = tmp_path / "financials_register" / "36319007.jsonl"
    assert out_file.exists(), f"Expected {out_file} to be written"

    rows = [json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
    assert rows, "JSONL must not be empty"


def test_build_sk_financials_from_files_row_fields(tmp_path):
    """All rows carry source='registeruz', country='SK', basis='company', currency='EUR'."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_sk_financials_from_files

    cfg = Config(data_dir=tmp_path)
    build_sk_financials_from_files(
        FIXTURES / "sk_36319007_POD.json",
        FIXTURES / "sk_sablona_699.json",
        config=cfg,
        write=True,
    )

    out_file = tmp_path / "financials_register" / "36319007.jsonl"
    rows = [json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
    for row in rows:
        assert row["source"] == "registeruz", f"unexpected source: {row['source']}"
        assert row["country"] == "SK", f"unexpected country: {row['country']}"
        assert row["basis"] == "company", f"unexpected basis: {row['basis']}"
        assert row["currency"] == "EUR", f"unexpected currency: {row['currency']}"
        assert row["period_end"] == "2023-12-31"
        assert row["fy"] == 2023


def test_build_sk_financials_from_files_equity_value(tmp_path):
    """Equity reported row carries value=262763.0."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_sk_financials_from_files

    cfg = Config(data_dir=tmp_path)
    build_sk_financials_from_files(
        FIXTURES / "sk_36319007_POD.json",
        FIXTURES / "sk_sablona_699.json",
        config=cfg,
        write=True,
    )

    rows = [json.loads(ln) for ln in
            (tmp_path / "financials_register" / "36319007.jsonl").read_text().splitlines()
            if ln.strip()]
    eq_rows = [r for r in rows if r["concept"] == "equity" and r["kind"] == "reported"]
    assert eq_rows, "equity reported row missing"
    assert eq_rows[0]["value"] == 262763.0


def test_build_sk_financials_from_files_debt_to_equity_present(tmp_path):
    """debt_to_equity derived row is present when bank loans are non-zero."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_sk_financials_from_files

    cfg = Config(data_dir=tmp_path)
    build_sk_financials_from_files(
        FIXTURES / "sk_36319007_POD.json",
        FIXTURES / "sk_sablona_699.json",
        config=cfg,
        write=True,
    )

    rows = [json.loads(ln) for ln in
            (tmp_path / "financials_register" / "36319007.jsonl").read_text().splitlines()
            if ln.strip()]
    derived_concepts = {r["concept"] for r in rows if r["kind"] == "derived"}
    assert "debt_to_equity" in derived_concepts, (
        f"debt_to_equity missing; derived: {sorted(derived_concepts)}"
    )


def test_build_sk_financials_from_files_leverage_basis_borrowings(tmp_path):
    """debt_to_equity and total_debt rows carry leverage_basis='borrowings'."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_sk_financials_from_files

    cfg = Config(data_dir=tmp_path)
    build_sk_financials_from_files(
        FIXTURES / "sk_36319007_POD.json",
        FIXTURES / "sk_sablona_699.json",
        config=cfg,
        write=True,
    )

    rows = [json.loads(ln) for ln in
            (tmp_path / "financials_register" / "36319007.jsonl").read_text().splitlines()
            if ln.strip()]
    dte_rows = [r for r in rows if r["concept"] == "debt_to_equity" and r["kind"] == "derived"]
    assert dte_rows, "debt_to_equity derived row missing"
    assert dte_rows[0]["leverage_basis"] == "borrowings", (
        f"expected 'borrowings', got {dte_rows[0].get('leverage_basis')!r}"
    )


def test_build_sk_financials_from_files_interest_coverage_present(tmp_path):
    """interest_coverage derived row is present (operating_income / interest_expense)."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_sk_financials_from_files

    cfg = Config(data_dir=tmp_path)
    build_sk_financials_from_files(
        FIXTURES / "sk_36319007_POD.json",
        FIXTURES / "sk_sablona_699.json",
        config=cfg,
        write=True,
    )

    rows = [json.loads(ln) for ln in
            (tmp_path / "financials_register" / "36319007.jsonl").read_text().splitlines()
            if ln.strip()]
    ic_rows = [r for r in rows if r["concept"] == "interest_coverage" and r["kind"] == "derived"]
    assert ic_rows, "interest_coverage derived row missing"


def test_build_sk_financials_from_files_nodebt_no_debt_to_equity(tmp_path):
    """No-debt fixture: zero bank loans → debt_to_equity absent, gate holds."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_sk_financials_from_files

    cfg = Config(data_dir=tmp_path)
    out = build_sk_financials_from_files(
        FIXTURES / "sk_50296353_POD_nodebt.json",
        FIXTURES / "sk_sablona_699.json",
        config=cfg,
        write=True,
    )

    assert out["with_financials"] == 1
    assert out["unbalanced"] == 0
    ico = "50296353"
    out_file = tmp_path / "financials_register" / f"{ico}.jsonl"
    assert out_file.exists()
    rows = [json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
    derived_concepts = {r["concept"] for r in rows if r["kind"] == "derived"}
    assert "debt_to_equity" not in derived_concepts, (
        f"debt_to_equity must be absent for no-debt filing; derived: {sorted(derived_concepts)}"
    )


def test_build_sk_financials_from_files_dry_run(tmp_path):
    """write=False: no file written, coverage_path=None, counters correct."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_sk_financials_from_files

    cfg = Config(data_dir=tmp_path)
    out = build_sk_financials_from_files(
        FIXTURES / "sk_36319007_POD.json",
        FIXTURES / "sk_sablona_699.json",
        config=cfg,
        write=False,
    )

    assert out["with_financials"] == 1
    assert out["paths"] == []
    assert out["coverage_path"] is None
    assert not (tmp_path / "financials_register" / "36319007.jsonl").exists()


def test_build_sk_financials_stubbed_fetcher(tmp_path):
    """build_sk_financials with a stubbed fetcher accumulates periods per IČO."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_sk_financials

    vykaz_data = json.loads((FIXTURES / "sk_36319007_POD.json").read_bytes())
    sablona_data = json.loads((FIXTURES / "sk_sablona_699.json").read_bytes())

    entity_resp = {"ico": "36319007", "idUctovnychZavierok": [1001]}
    zavierka_resp = {"id": 1001, "idUctovnychVykazov": [9001]}

    def get_json(url, *, params=None, **_):
        if "uctovna-jednotka" in url:
            return entity_resp
        if "uctovna-zavierka" in url:
            return zavierka_resp
        if "uctovny-vykaz" in url:
            return vykaz_data
        if "sablona" in url:
            return sablona_data
        raise ValueError(f"Unexpected URL: {url}")

    fetcher = MagicMock()
    fetcher.get_json.side_effect = get_json

    cfg = Config(data_dir=tmp_path)
    out = build_sk_financials([12345], fetcher=fetcher, config=cfg, write=True)

    assert out["with_financials"] == 1
    out_file = tmp_path / "financials_register" / "36319007.jsonl"
    assert out_file.exists()
    rows = [json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
    assert any(r["source"] == "registeruz" for r in rows)
    assert any(r["country"] == "SK" for r in rows)


# --- CLI -------------------------------------------------------------------

def test_cli_sk_file_dry_run(tmp_path):
    """register-financials --sk-file dry-run: rc=0, no JSONL written."""
    from company_corpus.cli import main

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--sk-file", str(FIXTURES / "sk_36319007_POD.json"),
        str(FIXTURES / "sk_sablona_699.json"),
    ])
    assert rc == 0
    out_file = tmp_path / "financials_register" / "36319007.jsonl"
    assert not out_file.exists(), "Dry-run must not write any file"


def test_cli_sk_file_write(tmp_path):
    """register-financials --sk-file --write: rc=0, JSONL written."""
    from company_corpus.cli import main

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--sk-file", str(FIXTURES / "sk_36319007_POD.json"),
        str(FIXTURES / "sk_sablona_699.json"),
        "--write",
    ])
    assert rc == 0
    out_file = tmp_path / "financials_register" / "36319007.jsonl"
    assert out_file.exists()
