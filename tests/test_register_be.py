"""Tests for the Belgium BNB CBSO register: the stdlib XBRL parser (no Arelle)
and the dimensional concept pack + NO-FALSE-DATA confidence gate."""
from pathlib import Path

import pytest

from company_corpus.registers.bnb_xbrl import parse_bnb_data_xbrl, open_bnb_deposit
from company_corpus.registers.concepts_be import map_bnb_facts, BE_PACK

_FIXTURES = Path(__file__).parent / "fixtures" / "be"
FIXTURE = str(_FIXTURES / "m02_full_0648822310.xbrl")
FIXTURE_M01 = str(_FIXTURES / "m01_abbrev_0508773215.xbrl")
FIXTURE_M07 = str(_FIXTURES / "m07_micro_0563659278.xbrl")


def _val(res, key):
    """The numeric value emitted for ``key`` (KeyError if suppressed/absent)."""
    return res["values"][key]["value"]


def test_parse_bnb_xbrl_count():
    facts = parse_bnb_data_xbrl(FIXTURE)
    assert len(facts) > 1000


def test_parse_bnb_xbrl_dims_keys():
    facts = parse_bnb_data_xbrl(FIXTURE)
    # At least one fact must have both "bas" and "part" dimension keys
    assert any("bas" in f["dims"] and "part" in f["dims"] for f in facts)


def test_parse_bnb_xbrl_total_assets_anchor():
    facts = parse_bnb_data_xbrl(FIXTURE)
    # Context c97: bas=m25, part=m1, prd=m1 → total assets 14 340 301 238.13 €
    target_dims = {"bas": "m25", "part": "m1", "prd": "m1"}
    matches = [f for f in facts if f["dims"] == target_dims]
    assert matches, f"No fact with dims == {target_dims!r}"
    values = [f["value"] for f in matches]
    assert any(abs(v - 14_340_301_238.13) < 1 for v in values), (
        f"Total-assets anchor not found; values seen: {values}"
    )


def test_open_bnb_deposit(tmp_path):
    import zipfile

    data_bytes = b"<xbrl>data</xbrl>"
    contact_bytes = b"<xbrl>contact</xbrl>"
    vendor_bytes = b"<xbrl>vendor</xbrl>"

    zip_path = tmp_path / "test_deposit.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("filing-contact.xbrl", contact_bytes)
        zf.writestr("filing-data.xbrl", data_bytes)
        zf.writestr("filing-vendor.xbrl", vendor_bytes)

    result = open_bnb_deposit(zip_path.read_bytes())
    assert result == data_bytes


# --------------------------------------------------------------------------- #
# Concept pack + confidence gate — asserted against the REAL m02 filing.       #
# --------------------------------------------------------------------------- #

def _map_fixture(path):
    return map_bnb_facts(parse_bnb_data_xbrl(path), period_end="2020-12-31")


def test_map_bnb_m02_core_values():
    """Every curated key on the real m02 filing == its validated total (±1 EUR)."""
    res = _map_fixture(FIXTURE)
    expected = {
        "equity": 6734534627,
        "revenue": 484777143,
        "assets": 14340301238,
        "liabilities": 7298623904,
        "provisions": 307142707,
        "income_tax": 36038793,
        "dep_amort": 63038416,
        "inventory": 4606684,
        "receivables": 232625124,
    }
    for key, want in expected.items():
        assert round(_val(res, key)) == want, f"{key}: {_val(res, key)!r} != {want}"
        assert res["values"][key]["unit"] == "EUR"
        assert res["values"][key]["label"] == key


def test_map_bnb_m02_net_income_is_total_not_pretax_trap():
    """net_income is the breakdown-free m59/m4 total (115.9M), NOT the m59/spec=m16
    pre-tax disaggregation (54.4M) — the false-data trap."""
    res = _map_fixture(FIXTURE)
    assert round(_val(res, "net_income")) == 115942395
    assert round(_val(res, "net_income")) != 54441434


def test_map_bnb_m02_financial_debt_block_emitted():
    """The m51 borrowings block is emitted (x-check reconciles vs m50[ntr=m3])."""
    res = _map_fixture(FIXTURE)
    assert round(_val(res, "long_term_debt")) == 2300987436   # 986217794 + 1314769642
    assert round(_val(res, "short_term_debt")) == 4338796392
    # engine total_debt = LT + ST = real borrowings
    assert round(_val(res, "long_term_debt") + _val(res, "short_term_debt")) == 6639783828
    assert res["values"]["long_term_debt"]["tag"] == "m51 (derived, x-checked)"


def test_map_bnb_m02_balanced_and_operating_profit_suppressed():
    res = _map_fixture(FIXTURE)
    assert res["unbalanced"] is False
    assert res["basis"] == "company"
    assert res["currency"] == "EUR"
    assert res["period_end"] == "2020-12-31"
    # operating_profit is ALWAYS suppressed (label ambiguous, pending 2nd example)
    assert "operating_profit" not in res["values"]
    assert any(k == "operating_profit" for k, _ in res["suppressed"])


@pytest.mark.parametrize("path", [FIXTURE_M01, FIXTURE_M07])
def test_map_bnb_small_models_equity_present_revenue_absent(path):
    """Abbreviated (m01) & micro (m07) models: parse must not crash; equity is
    present (~3.616M) but turnover is omitted by small models -> revenue absent."""
    res = _map_fixture(path)
    assert res is not None
    assert 3_616_000 <= round(_val(res, "equity")) <= 3_617_000
    assert "revenue" not in res["values"]
    assert res["unbalanced"] is False


def test_map_bnb_unbalanced_gate_blanks_values():
    """Synthetic: total assets (m25/m1) != total passif (m25/m3) beyond tol ->
    the whole filing is untrustworthy: unbalanced, no values emitted."""
    flat = [
        {"dims": {"bas": "m25", "part": "m1", "prd": "m1"}, "value": 1_000_000.0, "unit": "EUR"},
        {"dims": {"bas": "m25", "part": "m3", "prd": "m1"}, "value": 2_000_000.0, "unit": "EUR"},
        {"dims": {"bas": "m37", "part": "m3", "prd": "m1", "ntr": "m4"}, "value": 500_000.0, "unit": "EUR"},
    ]
    res = map_bnb_facts(flat)
    assert res["unbalanced"] is True
    assert res["values"] == {}
    assert res["suppressed"]  # a reason was recorded


def _balanced_base():
    """A minimal balanced flat (m25/m1 == m25/m3) carrying equity."""
    return [
        {"dims": {"bas": "m25", "part": "m1", "prd": "m1"}, "value": 100_000.0, "unit": "EUR"},
        {"dims": {"bas": "m25", "part": "m3", "prd": "m1"}, "value": 100_000.0, "unit": "EUR"},
        {"dims": {"bas": "m37", "part": "m3", "prd": "m1", "ntr": "m4"}, "value": 40_000.0, "unit": "EUR"},
    ]


def test_map_bnb_debt_block_suppressed_when_xcheck_fails():
    """Synthetic: m51 tranches that do NOT reconcile with m50[ntr=m3] ->
    the debt block is suppressed (reason recorded); other values still emit."""
    flat = _balanced_base() + [
        # m51 tranches sum to 3,000 ...
        {"dims": {"bas": "m51", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1", "typ": "m1"}, "value": 1_000.0, "unit": "EUR"},
        {"dims": {"bas": "m51", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m2", "typ": "m1"}, "value": 2_000.0, "unit": "EUR"},
        # ... but the independent m50[ntr=m3] witness says 20,000 -> mismatch.
        {"dims": {"bas": "m50", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1"}, "value": 10_000.0, "unit": "EUR"},
        {"dims": {"bas": "m50", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m2"}, "value": 10_000.0, "unit": "EUR"},
    ]
    res = map_bnb_facts(flat)
    assert "long_term_debt" not in res["values"]
    assert "short_term_debt" not in res["values"]
    assert any(k in ("long_term_debt", "short_term_debt") for k, _ in res["suppressed"])
    assert round(_val(res, "equity")) == 40_000          # other values still emitted
    assert res["unbalanced"] is False


def test_map_bnb_debt_block_suppressed_on_split_mismatch():
    """Fix 1 — per-bucket cross-check: m51 is ALL LT (rst=m1, total=1_000_000) but
    the m50[ntr=m3] witness splits 400_000 LT / 600_000 ST. The totals reconcile
    (1_000_000 == 400_000 + 600_000) but the LT bucket disagrees → the whole
    debt block must be SUPPRESSED (reason recorded). long_term_debt, short_term_debt,
    and total_debt must all be absent from the result."""
    flat = _balanced_base() + [
        # m51: all LT (rst=m1 tranches only), total = 1_000_000
        {"dims": {"bas": "m51", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1", "typ": "m1"}, "value": 600_000.0, "unit": "EUR"},
        {"dims": {"bas": "m51", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1", "typ": "m2"}, "value": 400_000.0, "unit": "EUR"},
        # m50[ntr=m3] witness: 400_000 LT + 600_000 ST = 1_000_000 (totals match,
        # but per-bucket disagrees: m51 LT=1_000_000 vs witness_lt=400_000)
        {"dims": {"bas": "m50", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1"}, "value": 400_000.0, "unit": "EUR"},
        {"dims": {"bas": "m50", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m2"}, "value": 600_000.0, "unit": "EUR"},
    ]
    res = map_bnb_facts(flat)
    # LT bucket disagrees → whole debt block suppressed
    assert "long_term_debt" not in res["values"]
    assert "short_term_debt" not in res["values"]
    assert "total_debt" not in res["values"]   # derived from LT+ST; absent if LT/ST absent
    # Reason recorded for at least one of the debt keys
    assert any(k in ("long_term_debt", "short_term_debt") for k, _ in res["suppressed"])
    # Non-debt values still emit; filing itself is not unbalanced
    assert round(_val(res, "equity")) == 40_000
    assert res["unbalanced"] is False


def test_map_bnb_debt_block_suppressed_when_tranche_lacks_typ():
    """Synthetic: an m51 balance-sheet fact missing its typ tranche -> the total
    cannot be confirmed, so the debt block is suppressed atomically."""
    flat = _balanced_base() + [
        # a breakdown-free m51 fact with rst but no typ (a deviating structure)
        {"dims": {"bas": "m51", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1"}, "value": 1_000.0, "unit": "EUR"},
        {"dims": {"bas": "m50", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1"}, "value": 1_000.0, "unit": "EUR"},
    ]
    res = map_bnb_facts(flat)
    assert "long_term_debt" not in res["values"]
    assert "short_term_debt" not in res["values"]
    assert any(k in ("long_term_debt", "short_term_debt") for k, _ in res["suppressed"])
    assert round(_val(res, "equity")) == 40_000


def test_be_pack_shape():
    """The pack is (bas, part, required-members) triples for the documented keys."""
    for key in ("assets", "equity", "revenue", "net_income", "income_tax",
                "dep_amort", "inventory", "receivables", "liabilities",
                "liabilities_current", "provisions", "cash", "non_current_assets",
                "assets_current"):
        assert key in BE_PACK
        bas, part, required = BE_PACK[key]
        assert isinstance(bas, str) and isinstance(part, str) and isinstance(required, dict)


# ---------------------------------------------------------------------------
# BE identity: _norm_kbo + resolve_register_specs BE branch
# ---------------------------------------------------------------------------

def test_norm_kbo_strips_dots():
    from company_corpus.registers.identity import _norm_kbo
    assert _norm_kbo("0648.822.310") == "0648822310"


def test_norm_kbo_zero_pads_to_10():
    from company_corpus.registers.identity import _norm_kbo
    assert _norm_kbo("417497106") == "0417497106"   # 9 digits -> pad to 10


class _GleifFetcherBE:
    """Minimal GLEIF stub returning one BE entity record."""
    def __init__(self, country, registered_as, name="AGEAS SA/NV"):
        self._c, self._r, self._n = country, registered_as, name

    def get_json(self, url, **kw):
        return {"data": {"attributes": {"entity": {
            "legalName": {"name": self._n},
            "legalAddress": {"country": self._c},
            "registeredAs": self._r,
        }}}}


def test_be_lei_resolves_via_gleif():
    """LEI for a BE entity resolves via GLEIF entity.registeredAs -> be_number (KBO)."""
    from company_corpus.registers.identity import resolve_register_specs
    r = resolve_register_specs(
        [{"lei": "L1BE"}],
        fetcher=_GleifFetcherBE("BE", "0417.497.106"),
    )[0]
    assert r["be_number"] == "0417497106"
    assert r["country"] == "BE"
    assert r["status"] == "ok"
    assert r["lei"] == "L1BE"


def test_non_be_lei_unresolved():
    """LEI for a non-BE entity (e.g. FR) stays unresolved; no be_number returned."""
    from company_corpus.registers.identity import resolve_register_specs
    r = resolve_register_specs(
        [{"lei": "L2FR"}],
        fetcher=_GleifFetcherBE("FR", "417497106"),
    )[0]
    assert r["status"] == "unresolved"
    assert not r.get("be_number")


# ---------------------------------------------------------------------------
# CBSO Authentic Data API acquisition — fetch_bnb_deposit
# ---------------------------------------------------------------------------

_CBSO_REFS = [
    {
        "ReferenceNumber": "REF001",
        "DepositDate": "2019-04-10",
        "ExerciseDates": {"StartDate": "2018-01-01", "EndDate": "2018-12-31"},
        "ModelType": "M02",
        "AccountingDataURL": "https://ws.cbso.nbb.be/authentic/deposit/REF001/accountingData",
    },
    {
        "ReferenceNumber": "REF002",
        "DepositDate": "2021-03-15",
        "ExerciseDates": {"StartDate": "2020-01-01", "EndDate": "2020-12-31"},
        "ModelType": "M02",
        "AccountingDataURL": "https://ws.cbso.nbb.be/authentic/deposit/REF002/accountingData",
    },
]

_FIXTURE_BYTES = (Path(__file__).parent / "fixtures/be/m02_full_0648822310.xbrl").read_bytes()


class _CbsoFetcherOK:
    """Stub: get_json returns refs; get() returns bytes by URL (or fallback acct_bytes)."""

    def __init__(self, refs=_CBSO_REFS, acct_bytes=_FIXTURE_BYTES, url_map=None):
        self._refs = refs
        self._acct = acct_bytes
        self._url_map: dict[str, bytes] = url_map or {}
        self.last_get_json_headers = None
        self.last_get_headers = None
        self.last_get_url = None

    def get_json(self, url, *, headers=None, **kw):
        self.last_get_json_headers = headers
        return self._refs

    def get(self, url, *, headers=None, **kw):
        self.last_get_headers = headers
        self.last_get_url = url

        class _Resp:
            pass

        r = _Resp()
        r.content = self._url_map.get(url, self._acct)
        return r


class _CbsoFetcherRefsError:
    """Stub: get_json raises (simulates network / auth failure)."""

    def get_json(self, url, **kw):
        raise RuntimeError("network error")


def test_fetch_bnb_deposit_latest_bytes():
    """Two deposits (2019-deposit vs 2021-deposit) → bytes for the LATEST URL returned.

    REF001 has DepositDate 2019-04-10 (older); REF002 has DepositDate 2021-03-15 (latest).
    The stub returns distinct bytes per URL so this test fails if _pick_latest
    selects the wrong deposit.
    """
    from company_corpus.registers.bnb_cbso import fetch_bnb_deposit

    _URL_MAP = {
        "https://ws.cbso.nbb.be/authentic/deposit/REF001/accountingData": b"OLDER",
        "https://ws.cbso.nbb.be/authentic/deposit/REF002/accountingData": b"LATEST",
    }
    fetcher = _CbsoFetcherOK(url_map=_URL_MAP)
    result = fetch_bnb_deposit("0648822310", fetcher=fetcher, key="test-key")
    assert result == b"LATEST"


def test_fetch_bnb_deposit_headers_sent():
    """Subscription-key and X-Request-Id headers are forwarded on both calls."""
    from company_corpus.registers.bnb_cbso import fetch_bnb_deposit

    fetcher = _CbsoFetcherOK()
    fetch_bnb_deposit("0648822310", fetcher=fetcher, key="my-api-key")

    assert fetcher.last_get_json_headers["NBB-CBSO-Subscription-Key"] == "my-api-key"
    assert "X-Request-Id" in fetcher.last_get_json_headers
    assert fetcher.last_get_headers["NBB-CBSO-Subscription-Key"] == "my-api-key"
    assert "X-Request-Id" in fetcher.last_get_headers
    assert fetcher.last_get_headers.get("Accept") == "application/x.xbrl"


def test_fetch_bnb_deposit_empty_refs_returns_none():
    """Empty references list → None (batch-safe)."""
    from company_corpus.registers.bnb_cbso import fetch_bnb_deposit

    result = fetch_bnb_deposit("0000000000", fetcher=_CbsoFetcherOK(refs=[]), key="k")
    assert result is None


def test_fetch_bnb_deposit_refs_error_returns_none():
    """get_json raising → None (batch-safe, never raises)."""
    from company_corpus.registers.bnb_cbso import fetch_bnb_deposit

    result = fetch_bnb_deposit("0648822310", fetcher=_CbsoFetcherRefsError(), key="k")
    assert result is None


def test_fetch_bnb_deposit_acct_error_returns_none():
    """get() raising on accounting-data URL → None (batch-safe)."""
    from company_corpus.registers.bnb_cbso import fetch_bnb_deposit

    class _AcErrFetcher(_CbsoFetcherOK):
        def get(self, url, **kw):
            raise RuntimeError("download failed")

    result = fetch_bnb_deposit("0648822310", fetcher=_AcErrFetcher(), key="k")
    assert result is None


# ---------------------------------------------------------------------------
# Task 5 additions: period_end_of, dep_amort rename, tranche-dedup, producers,
# and CLI. All tests are written BEFORE their implementations (TDD Red phase).
# ---------------------------------------------------------------------------

# --- A. period_end_of -------------------------------------------------------

def test_period_end_of_m02_returns_2020_12_31():
    """period_end_of returns the max period date from the m02 fixture = 2020-12-31."""
    from company_corpus.registers.bnb_xbrl import period_end_of
    result = period_end_of(FIXTURE)
    assert result == "2020-12-31"


def test_period_end_of_from_bytes():
    """period_end_of also works when passed raw bytes."""
    from company_corpus.registers.bnb_xbrl import period_end_of
    data = Path(FIXTURE).read_bytes()
    result = period_end_of(data)
    assert result == "2020-12-31"


def test_period_end_of_empty_returns_none():
    """period_end_of returns None when no period dates are found."""
    from company_corpus.registers.bnb_xbrl import period_end_of
    minimal = b"<xbrl><context id='c1'><entity/></context></xbrl>"
    assert period_end_of(minimal) is None


# --- D1. dep_amort rename ---------------------------------------------------

def test_dep_amort_in_be_pack_not_depreciation():
    """After the rename dep_amort is the pack key; depreciation must not appear."""
    assert "dep_amort" in BE_PACK
    assert "depreciation" not in BE_PACK


def test_dep_amort_present_in_mapped_values():
    """map_bnb_facts emits dep_amort (not depreciation) for the m02 fixture."""
    res = _map_fixture(FIXTURE)
    assert "dep_amort" in res["values"]
    assert "depreciation" not in res["values"]
    assert round(_val(res, "dep_amort")) == 63038416


# --- D2. Tranche-dedup hardening -------------------------------------------

def test_tranche_dedup_prevents_double_count():
    """Duplicate m51 facts with identical dim-tuples must only count once (not
    double-count the borrowings total)."""
    flat = _balanced_base() + [
        # This tranche appears TWICE with the same dims — a malformed filing.
        {"dims": {"bas": "m51", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1", "typ": "m1"}, "value": 1_000.0, "unit": "EUR"},
        {"dims": {"bas": "m51", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1", "typ": "m1"}, "value": 1_000.0, "unit": "EUR"},  # duplicate
        {"dims": {"bas": "m51", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m2", "typ": "m1"}, "value": 500.0, "unit": "EUR"},
        # Witness reconciles against deduplicated sum (1000+500=1500).
        {"dims": {"bas": "m50", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1"}, "value": 1_000.0, "unit": "EUR"},
        {"dims": {"bas": "m50", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m2"}, "value": 500.0, "unit": "EUR"},
    ]
    res = map_bnb_facts(flat)
    # After dedup: LT=1000, ST=500, total=1500 — matches witness, so emitted.
    assert round(_val(res, "long_term_debt")) == 1_000
    assert round(_val(res, "short_term_debt")) == 500


def test_witness_dedup_prevents_double_count():
    """Duplicate m50[ntr=m3] witness facts with identical dim-tuples must only
    count once so the cross-check is not falsely satisfied."""
    flat = _balanced_base() + [
        {"dims": {"bas": "m51", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1", "typ": "m1"}, "value": 1_000.0, "unit": "EUR"},
        {"dims": {"bas": "m51", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m2", "typ": "m1"}, "value": 500.0, "unit": "EUR"},
        # Witness says 1500 total but with a DUPLICATE — without dedup it would sum to 3000.
        {"dims": {"bas": "m50", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1"}, "value": 1_000.0, "unit": "EUR"},
        {"dims": {"bas": "m50", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m1"}, "value": 1_000.0, "unit": "EUR"},  # duplicate
        {"dims": {"bas": "m50", "part": "m3", "prd": "m1", "ntr": "m3", "rst": "m2"}, "value": 500.0, "unit": "EUR"},
    ]
    res = map_bnb_facts(flat)
    # After dedup: borrow=1500, witness=1500 → emitted.
    assert round(_val(res, "long_term_debt")) == 1_000
    assert round(_val(res, "short_term_debt")) == 500


# --- B. Producers -----------------------------------------------------------

def test_build_be_financials_from_files_writes_jsonl(tmp_path):
    """from_files with write=True writes data/financials_register/0648822310.jsonl
    and rows carry the expected identity + values."""
    import json
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_be_financials_from_files

    cfg = Config(data_dir=tmp_path)
    out = build_be_financials_from_files([FIXTURE], config=cfg, write=True)

    # Summary counters
    assert out["entities"] == 1
    assert out["with_financials"] == 1
    assert out["no_financials"] == 0
    assert out["errors"] == 0

    # File written
    out_file = tmp_path / "financials_register" / "0648822310.jsonl"
    assert out_file.exists(), f"Expected {out_file} to be written"

    rows = [json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
    assert rows, "JSONL must not be empty"

    # Identity columns on every row
    for row in rows:
        assert row["source"] == "bnb"
        assert row["country"] == "BE"
        assert row["basis"] == "company"
        assert row["period_end"] == "2020-12-31"
        assert row["fy"] == 2020
        assert row["currency"] == "EUR"

    # Reported equity present
    eq_rows = [r for r in rows if r["concept"] == "equity" and r["kind"] == "reported"]
    assert eq_rows, "equity row missing"
    assert round(eq_rows[0]["value"]) == 6_734_534_627

    # dep_amort present (not depreciation)
    dep_rows = [r for r in rows if r["concept"] == "dep_amort" and r["kind"] == "reported"]
    assert dep_rows, "dep_amort row missing"
    assert "depreciation" not in {r["concept"] for r in rows}

    # Borrowings-based debt_to_equity derived concept present
    dte_rows = [r for r in rows if r["concept"] == "debt_to_equity" and r["kind"] == "derived"]
    assert dte_rows, "debt_to_equity derived row missing"

    # total_debt derived = 6_639_783_828
    td_rows = [r for r in rows if r["concept"] == "total_debt" and r["kind"] == "derived"]
    assert td_rows, "total_debt derived row missing"
    assert round(td_rows[0]["value"]) == 6_639_783_828

    # C1: BE leverage is borrowings-based (real m51 financial debt) -> stamped.
    assert dte_rows[0]["leverage_basis"] == "borrowings"
    assert td_rows[0]["leverage_basis"] == "borrowings"


def test_build_be_financials_from_files_dry_run(tmp_path):
    """write=False (dry-run): no file written, but counters correct."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_be_financials_from_files

    cfg = Config(data_dir=tmp_path)
    out = build_be_financials_from_files([FIXTURE], config=cfg, write=False)

    assert out["with_financials"] == 1
    out_file = tmp_path / "financials_register" / "0648822310.jsonl"
    assert not out_file.exists(), "Dry-run must not write any file"
    assert out["paths"] == []


def test_build_be_financials_from_files_error_isolation(tmp_path):
    """A path that raises must be counted as an error without aborting the batch."""
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_be_financials_from_files

    cfg = Config(data_dir=tmp_path)
    out = build_be_financials_from_files(
        ["/nonexistent/path/0123456789.xbrl", FIXTURE],
        config=cfg, write=False,
    )
    assert out["errors"] == 1
    assert out["with_financials"] == 1


def test_build_be_financials_api_stub(tmp_path):
    """API path: stubbed fetcher returning fixture bytes → same rows as from_files."""
    import json
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_be_financials

    cfg = Config(data_dir=tmp_path)
    out = build_be_financials(
        [{"be_number": "0648822310"}],
        fetcher=_CbsoFetcherOK(),
        config=cfg,
        key="test-key",
        write=True,
    )

    assert out["entities"] == 1
    assert out["with_financials"] == 1
    assert out["errors"] == 0

    out_file = tmp_path / "financials_register" / "0648822310.jsonl"
    assert out_file.exists()
    rows = [json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
    assert any(r["concept"] == "equity" and r["kind"] == "reported" for r in rows)
    assert any(r["source"] == "bnb" for r in rows)
    assert any(r["country"] == "BE" for r in rows)


# --- C. CLI -----------------------------------------------------------------

def test_cli_be_file_dry_run(tmp_path):
    """--be-file dry-run: build_be_financials_from_files called with write=False."""
    from company_corpus.cli import main

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--be-file", FIXTURE,
    ])
    assert rc == 0
    # Dry-run: no file written
    out_file = tmp_path / "financials_register" / "0648822310.jsonl"
    assert not out_file.exists()


def test_cli_be_file_write(tmp_path):
    """--be-file --write: writes the JSONL."""
    from company_corpus.cli import main

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--be-file", FIXTURE,
        "--write",
    ])
    assert rc == 0
    out_file = tmp_path / "financials_register" / "0648822310.jsonl"
    assert out_file.exists()


def test_cli_be_numbers_dry_run(tmp_path, monkeypatch):
    """--be-numbers dry-run is fully offline: _fetch_bnb_deposit is monkeypatched.

    Without the patch the CLI would reach the real https://ws.cbso.nbb.be
    API (retries → up to ~127 s in offline CI).  With the patch:
    - fetch returns None → 1 entity counted as no-financials (not an error)
    - rc == 0 (CLI succeeds)
    - no file written (dry-run)
    - fetch was called with exactly the KBO we supplied
    """
    import company_corpus.registers.financials as _rf
    from company_corpus.cli import main

    calls: list[str] = []

    def _stub_fetch(be_number, *, fetcher, key):
        calls.append(be_number)
        return None  # simulates "no deposit found" — batch-safe, no network

    monkeypatch.setattr(_rf, "_fetch_bnb_deposit", _stub_fetch)
    monkeypatch.setenv("BNB_CBSO_KEY", "dummy")

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--be-numbers", "0648822310",
    ])

    assert rc == 0
    # Dry-run: no JSONL written
    assert not (tmp_path / "financials_register" / "0648822310.jsonl").exists()
    # Stub was invoked exactly once with the requested KBO — confirms the CLI
    # reached the fetch call and didn't short-circuit before it.
    assert calls == ["0648822310"]
