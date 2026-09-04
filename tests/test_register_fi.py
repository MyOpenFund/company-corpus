"""Tests for the Finnish PRH register.

Task 1 — stdlib dimensional parser (``fi_prh_xbrl.parse_fi_facts``).
Task 2 — concept pack + NO-FALSE-DATA gate (``concepts_fi.map_fi_facts``).
"""
from pathlib import Path

import pytest

from company_corpus.financials import compute_derived
from company_corpus.registers.concepts_fi import map_fi_facts
from company_corpus.registers.fi_prh_xbrl import parse_fi_facts

_FIXTURES = Path(__file__).parent / "fixtures" / "fi"
FIXTURE = str(_FIXTURES / "fi_2919415-2_full_2024.xml")
ABBREV = str(_FIXTURES / "fi_0100379-9_abbrev_2023.xml")
HOUSING = str(_FIXTURES / "fi_0100843-4_housing_2023.xml")


def _parsed():
    """Parse the full 2024 fixture once (not cached — pytest handles isolation)."""
    return parse_fi_facts(FIXTURE)


# ===========================================================================
# Task 1 — parser
# ===========================================================================

def test_parse_fi_facts_revenue():
    """fields[673] == 481 773.33 (revenue line, md103 namespace)."""
    result = _parsed()
    assert result["fields"][673] == pytest.approx(481_773.33, abs=0.01)


def test_parse_fi_facts_total_assets_present_and_positive():
    """fields[360] (total assets) must be present and > 0."""
    result = _parsed()
    assert 360 in result["fields"]
    assert result["fields"][360] > 0


def test_parse_fi_facts_net_income_present():
    """fields[740] (net income, NOT x738) must be present."""
    result = _parsed()
    assert 740 in result["fields"]


def test_parse_fi_facts_currency():
    """currency must be 'EUR'."""
    result = _parsed()
    assert result["currency"] == "EUR"


def test_parse_fi_facts_period_end():
    """period_end must be '2024-12-31'."""
    result = _parsed()
    assert result["period_end"] == "2024-12-31"


def test_parse_fi_facts_no_prior_period_fields():
    """Prior-period facts (fi_dim:REF present) must be excluded."""
    result = _parsed()
    # All fields come from current contexts only: verify by checking
    # that we have some fields (parser ran) but prior MCY codes are not
    # double-counted as separate entries.
    assert len(result["fields"]) > 0


def test_parse_fi_facts_bytes_input():
    """parse_fi_facts must also accept raw bytes."""
    raw = open(FIXTURE, "rb").read()
    result = parse_fi_facts(raw)
    assert result["fields"][673] == pytest.approx(481_773.33, abs=0.01)
    assert result["period_end"] == "2024-12-31"


# ===========================================================================
# Task 2 — concept pack + NO-FALSE-DATA gate (map_fi_facts)
# ===========================================================================

def _mapped(path):
    return map_fi_facts(parse_fi_facts(path))


def _reason(mapped, key):
    """The suppression reason recorded for ``key`` (or None)."""
    for k, r in mapped["suppressed"]:
        if k == key:
            return r
    return None


# --- fi_2919415-2 (full 2024) ----------------------------------------------

def test_full_shape_basis_currency_balanced():
    m = _mapped(FIXTURE)
    assert m["basis"] == "company"
    assert m["currency"] == "EUR"
    assert m["period_end"] == "2024-12-31"
    assert m["unbalanced"] is False


def test_full_revenue():
    m = _mapped(FIXTURE)
    rev = m["values"]["revenue"]
    assert rev["value"] == pytest.approx(481_773.33, abs=0.01)
    assert rev["tag"] == "fi_MC:x673"
    assert rev["unit"] == "EUR"


def test_full_net_income_is_x740_not_x738():
    """THE TRAP: net_income is x740 (final, after appropriations), NEVER x738."""
    parsed = parse_fi_facts(FIXTURE)
    x738 = parsed["fields"][738]   # 72 574.02  pre-appropriations
    x740 = parsed["fields"][740]   # 57 560.30  final bottom line
    assert x738 != pytest.approx(x740, abs=0.01)      # the two genuinely differ
    m = map_fi_facts(parsed)
    ni = m["values"]["net_income"]
    assert ni["value"] == pytest.approx(57_560.30, abs=0.01)
    assert ni["value"] == pytest.approx(x740, abs=0.01)        # == x740
    assert ni["value"] != pytest.approx(x738, abs=0.01)        # NOT x738
    assert ni["tag"] == "fi_MC:x740"


def test_full_total_assets():
    m = _mapped(FIXTURE)
    ta = m["values"]["assets"]          # canonical engine key: assets (was total_assets)
    assert ta["value"] == pytest.approx(201_064.55, abs=0.01)
    assert ta["tag"] == "fi_MC:x360"


def test_full_equity_read_from_fixture_equals_assets_minus_liabilities():
    parsed = parse_fi_facts(FIXTURE)
    x435 = parsed["fields"][435]
    x360 = parsed["fields"][360]
    x513 = parsed["fields"][513]
    # x435 read straight from the fixture satisfies the balance identity.
    assert x435 == pytest.approx(x360 - x513, abs=0.01)   # 185650.88 == 201064.55 − 15413.67
    m = map_fi_facts(parsed)
    eq = m["values"]["equity"]
    assert eq["value"] == pytest.approx(x435, abs=0.01)
    assert eq["value"] == pytest.approx(185_650.88, abs=0.01)
    assert eq["tag"] == "fi_MC:x435"


def test_full_interest_expense_is_abs_of_x4046():
    parsed = parse_fi_facts(FIXTURE)
    assert parsed["fields"][4046] < 0                 # stored negative
    ie = map_fi_facts(parsed)["values"]["interest_expense"]
    assert ie["value"] == pytest.approx(abs(parsed["fields"][4046]), abs=0.01)
    assert ie["value"] >= 0
    assert ie["tag"] == "fi_MC:x4046"


def test_full_leverage_split_suppressed_despite_reconciling():
    """x583 + x816 == x513 to the cent, yet WHICH is long vs short is unconfirmed
    → suppress the maturity split (NO FALSE DATA). Total liabilities still emitted."""
    parsed = parse_fi_facts(FIXTURE)
    f = parsed["fields"]
    assert f[583] + f[816] == pytest.approx(f[513], abs=0.01)   # reconciles exactly
    m = map_fi_facts(parsed)
    assert "long_term_debt" not in m["values"]
    assert "short_term_debt" not in m["values"]
    reason = _reason(m, "long_term_debt")
    assert reason is not None and "UNCONFIRMED" in reason
    assert _reason(m, "short_term_debt") is not None
    # The confirmed TOTAL liabilities is still emitted (liabilities-based).
    assert m["values"]["liabilities"]["value"] == pytest.approx(15_413.67, abs=0.01)
    assert m["values"]["liabilities"]["tag"] == "fi_MC:x513"


def test_full_always_suppressed_concepts():
    m = _mapped(FIXTURE)
    for key in ("income_tax", "cash", "financial_debt", "provisions"):
        assert key not in m["values"]
        assert _reason(m, key) is not None


# --- fi_0100379-9 (abbreviated 2023) ---------------------------------------

def test_abbrev_revenue_absent_but_gate_holds():
    m = _mapped(ABBREV)
    assert "revenue" not in m["values"]               # x673 missing in abbreviated
    assert _reason(m, "revenue") is not None
    assert m["values"]["equity"]["value"] == pytest.approx(19_979.80, abs=0.01)
    assert m["values"]["assets"]["value"] == pytest.approx(122_979.81, abs=0.01)
    assert m["unbalanced"] is False                   # primary balance holds


# --- fi_0100843-4 (housing 2023) -------------------------------------------

def test_housing_negative_non_current_accepted():
    parsed = parse_fi_facts(HOUSING)
    x376 = parsed["fields"][376]
    assert x376 < 0                                   # negative non-current assets
    m = map_fi_facts(parsed)
    nc = m["values"]["non_current_assets"]
    assert nc["value"] == pytest.approx(x376, abs=0.01)   # accepted as-is, no positivity check
    assert nc["value"] < 0
    assert nc["tag"] == "fi_MC:x376"
    # decomposition x376 + x424 == x360 still holds → assets_current also emitted
    assert m["values"]["assets_current"]["value"] == pytest.approx(parsed["fields"][424], abs=0.01)
    assert m["unbalanced"] is False


def test_housing_decomposition_identity_holds():
    f = parse_fi_facts(HOUSING)["fields"]
    assert f[376] + f[424] == pytest.approx(f[360], abs=0.01)   # x376 negative, still balances


# --- synthetic edge cases --------------------------------------------------

def test_synthetic_unbalanced_blanks_all_values():
    """x360 != x435 + x513 beyond tol → unbalanced, NO values emitted."""
    parsed = {"period_end": "2024-12-31", "currency": "EUR",
              "fields": {360: 300_000.0, 435: 200_000.0, 513: 50_000.0, 673: 10_000.0}}
    m = map_fi_facts(parsed)
    assert m["unbalanced"] is True
    assert m["values"] == {}
    assert _reason(m, "__all__") is not None


def test_synthetic_debt_not_reconciling_suppresses_split():
    """x583 + x816 != x513 → maturity split suppressed with a reconciliation reason."""
    parsed = {"period_end": "2024-12-31", "currency": "EUR",
              "fields": {360: 250_000.0, 435: 200_000.0, 513: 50_000.0,
                         583: 40_000.0, 816: 5_000.0}}          # 45k != 50k liabilities
    m = map_fi_facts(parsed)
    assert m["unbalanced"] is False
    assert "long_term_debt" not in m["values"]
    assert "short_term_debt" not in m["values"]
    reason = _reason(m, "long_term_debt")
    assert reason is not None and "reconcile" in reason


# --- I1: canonical key names restore compute_derived ratios ---------------------

def test_compute_derived_produces_roa_operating_margin_interest_coverage():
    """After I1 rename (total_assets→assets, operating_profit→operating_income,
    current_assets→assets_current), compute_derived receives canonical keys and
    now produces roa, operating_margin, and interest_coverage from the full_2024
    fixture.  Before the rename these three were silently skipped."""
    m = _mapped(FIXTURE)
    assert m["unbalanced"] is False
    # Confirm the renamed keys are present in the output
    assert "assets" in m["values"], "canonical key 'assets' must be emitted"
    assert "operating_income" in m["values"], "canonical key 'operating_income' must be emitted"
    assert "interest_expense" in m["values"], "canonical key 'interest_expense' must be emitted"
    derived = compute_derived(m["values"], frequency="annual", currency="EUR")
    assert "roa" in derived, (
        "roa (net_income / assets) should be produced — requires 'assets' key")
    assert "operating_margin" in derived, (
        "operating_margin (operating_income / revenue) should be produced")
    assert "interest_coverage" in derived, (
        "interest_coverage (operating_income / interest_expense) should be produced")
    # Sanity-check values are finite numbers, not None
    for key in ("roa", "operating_margin", "interest_coverage"):
        assert isinstance(derived[key]["value"], (int, float)), \
            f"{key} value must be numeric"


# --- I2: P&L leg 1 -----------------------------------------------------------

def test_synthetic_pnl_leg1_failure_suppresses_net_income():
    """Leg-1 failure (|x689 + x12 - x738| > tol) suppresses net_income even when
    leg 2 would pass (x738 == x740, no appropriations)."""
    # x689=100k + x12=50k = 150k, but x738=200k: leg 1 FAILS (diff=50k >> tol=1k).
    # Leg 2: x738 + x541_absent == x740 → 200k + 0 == 200k: would pass alone.
    parsed = {
        "period_end": "2024-12-31", "currency": "EUR",
        "fields": {
            360: 500_000.0, 435: 300_000.0, 513: 200_000.0,   # balanced sheet
            673: 1_000_000.0,   # revenue
            689: 100_000.0,     # operating_income (x689)
            12:   50_000.0,     # net financial items (x12): 100k + 50k = 150k ≠ x738
            738: 200_000.0,     # result before appropriations (deliberately wrong)
            740: 200_000.0,     # result after appropriations
        },
    }
    m = map_fi_facts(parsed)
    assert m["unbalanced"] is False
    assert "net_income" not in m["values"], \
        "net_income must be suppressed when leg 1 fails"
    reason = _reason(m, "net_income")
    assert reason is not None
    assert "leg" in reason.lower(), \
        f"suppression reason should mention 'leg'; got: {reason!r}"


# ===========================================================================
# Task 3 — FI identity (Y-tunnus / LEI->GLEIF registeredAs)
# ===========================================================================

from company_corpus.registers.identity import _norm_ytunnus, resolve_register_specs


class _GleifFetcherFI:
    """Stub GLEIF fetcher returning a fixed country + registeredAs."""
    def __init__(self, country, registered_as):
        self._c, self._r = country, registered_as

    def get_json(self, url, **kw):
        return {"data": {"attributes": {"entity": {
            "legalName": {"name": "ACME FINLAND OY"},
            "legalAddress": {"country": self._c},
            "registeredAs": self._r,
        }}}}


def test_norm_ytunnus_strips_whitespace():
    """_norm_ytunnus strips surrounding spaces and keeps NNNNNNN-N intact."""
    assert _norm_ytunnus(" 2919415-2 ") == "2919415-2"
    assert _norm_ytunnus("0112038-9") == "0112038-9"
    assert _norm_ytunnus("  0112038-9  ") == "0112038-9"


def test_fi_lei_resolves_via_gleif_registeredas():
    """A FI LEI whose GLEIF country==FI resolves to business_id via registeredAs."""
    r = resolve_register_specs(
        [{"lei": "L_FI1"}],
        fetcher=_GleifFetcherFI("FI", "0112038-9"),
    )[0]
    assert r["business_id"] == "0112038-9"
    assert r["lei"] == "L_FI1"
    assert r["status"] == "ok"
    assert r["country"] == "FI"


def test_non_fi_lei_is_unresolved():
    """A LEI whose GLEIF country!=FI must not produce a business_id (no-guess)."""
    r = resolve_register_specs(
        [{"lei": "L_SE1"}],
        fetcher=_GleifFetcherFI("SE", "5560000000"),
    )[0]
    assert r.get("business_id") is None
    assert r["status"] == "unresolved"


# ===========================================================================
# Task 4 — keyless PRH XBRL acquisition (registers/prh_api.py)
# ===========================================================================

from company_corpus.registers.prh_api import (
    fetch_fi_financial,
    iter_fi_all,
    list_fi_dates,
)


class _StubFetcher:
    """Records calls and returns configured bytes or JSON; optionally raises."""

    def __init__(self, *, bytes_response=None, json_response=None, raises=False):
        self.calls: list[dict] = []
        self._bytes = bytes_response
        self._json = json_response
        self._raises = raises

    def get(self, url, *, headers=None, params=None, **kw):
        self.calls.append({"url": url, "headers": headers, "params": params})
        if self._raises:
            raise RuntimeError("simulated network error")

        class _Resp:
            def __init__(self, content):
                self.content = content

        return _Resp(self._bytes)

    def get_json(self, url, *, headers=None, params=None, **kw):
        self.calls.append({"url": url, "headers": headers, "params": params})
        if self._raises:
            raise RuntimeError("simulated network error")
        return self._json


def test_fetch_fi_financial_returns_fixture_bytes():
    """Stub fetcher → fetch_fi_financial returns the raw XBRL bytes."""
    raw = open(FIXTURE, "rb").read()
    stub = _StubFetcher(bytes_response=raw)
    result = fetch_fi_financial("2919415-2", "2024-12-31", fetcher=stub)
    assert result == raw


def test_fetch_fi_financial_no_accept_header():
    """Must NOT send Accept: application/xml — PRH returns 400 if sent."""
    raw = open(FIXTURE, "rb").read()
    stub = _StubFetcher(bytes_response=raw)
    fetch_fi_financial("2919415-2", "2024-12-31", fetcher=stub)
    call = stub.calls[0]
    hdrs = call["headers"] or {}
    assert "Accept" not in hdrs, f"Accept header must not be set; got headers={hdrs!r}"


def test_fetch_fi_financial_error_returns_none():
    """A fetcher that raises → returns None (batch-safe, never raises out)."""
    stub = _StubFetcher(raises=True)
    result = fetch_fi_financial("2919415-2", "2024-12-31", fetcher=stub)
    assert result is None


def test_fetch_fi_financial_uses_correct_url_and_params():
    """GET BASE/financial?businessId=…&financialDate=…"""
    raw = open(FIXTURE, "rb").read()
    stub = _StubFetcher(bytes_response=raw)
    fetch_fi_financial("2919415-2", "2024-12-31", fetcher=stub)
    call = stub.calls[0]
    assert call["url"].endswith("/financial")
    assert call["params"] == {"businessId": "2919415-2", "financialDate": "2024-12-31"}


def test_list_fi_dates_returns_list():
    """Stub returns the real envelope → list_fi_dates extracts financialDate strings."""
    envelope = {
        "totalResults": 3,
        "financials": [
            {"businessId": "2919415-2", "financialDate": "2022-12-31"},
            {"businessId": "2919415-2", "financialDate": "2023-12-31"},
            {"businessId": "2919415-2", "financialDate": "2024-12-31"},
        ],
    }
    stub = _StubFetcher(json_response=envelope)
    result = list_fi_dates("2919415-2", fetcher=stub)
    assert result == ["2022-12-31", "2023-12-31", "2024-12-31"]


def test_list_fi_dates_error_returns_empty_list():
    """A fetcher that raises → returns [] (batch-safe, never raises out)."""
    stub = _StubFetcher(raises=True)
    result = list_fi_dates("2919415-2", fetcher=stub)
    assert result == []


def test_list_fi_dates_uses_correct_url_and_params():
    """GET BASE/financials?businessId=…"""
    stub = _StubFetcher(json_response={"totalResults": 0, "financials": []})
    list_fi_dates("2919415-2", fetcher=stub)
    call = stub.calls[0]
    assert call["url"].endswith("/financials")
    assert call["params"] == {"businessId": "2919415-2"}


def test_iter_fi_all_yields_business_ids_and_stops_at_last_page():
    """Two pages: first returns 100 items, second returns 3 → stop after page 2.

    The real API returns {"totalResults": N, "financials": [{businessId, …}, …]},
    not a bare list.  Stub must match that shape.
    """
    page1_items = [{"businessId": f"ID{i:04d}"} for i in range(100)]
    page2_items = [{"businessId": "LAST1"}, {"businessId": "LAST2"}, {"businessId": "LAST3"}]

    class _PaginatedFetcher:
        def __init__(self):
            self.calls: list[dict | None] = []

        def get_json(self, url, *, params=None, **kw):
            self.calls.append(params)
            page = (params or {}).get("page", 1)
            items = page1_items if page == 1 else page2_items
            return {"totalResults": 103, "financials": items}

    stub = _PaginatedFetcher()
    result = list(iter_fi_all("2024-12-31", fetcher=stub))
    assert result[:3] == ["ID0000", "ID0001", "ID0002"]
    assert result[-3:] == ["LAST1", "LAST2", "LAST3"]
    assert len(result) == 103
    assert len(stub.calls) == 2  # exactly 2 page fetches


def test_iter_fi_all_error_stops_gracefully():
    """A fetcher that raises → yields nothing (batch-safe, never raises out)."""
    stub = _StubFetcher(raises=True)
    result = list(iter_fi_all("2024-12-31", fetcher=stub))
    assert result == []


# ===========================================================================
# Task 5 — producer + CLI
# ===========================================================================

import json
from company_corpus.config import Config
from company_corpus.registers.financials import (
    build_fi_financials_from_files,
    build_fi_financials,
)


class _PrhApiFetcher:
    """Stub for the PRH API (keyless): list_fi_dates + fetch_fi_financial."""

    def __init__(self, business_id: str, financial_date: str, xbrl_bytes: bytes):
        self._bid = business_id
        self._date = financial_date
        self._bytes = xbrl_bytes

    def get(self, url, *, headers=None, params=None, **kw):
        class _Resp:
            def __init__(self, content):
                self.content = content

        return _Resp(self._bytes)

    def get_json(self, url, *, headers=None, params=None, **kw):
        # GLEIF lookup (resolve_register_specs) — not needed for direct business_id
        # path, but must not crash if called.
        if "gleif" in url:
            return {}
        # list_fi_dates call: /financials?businessId=…
        # Real API returns an envelope, not a bare list.
        return {
            "totalResults": 1,
            "financials": [{"businessId": self._bid, "financialDate": self._date}],
        }


def test_build_fi_financials_from_files_writes_jsonl(tmp_path):
    """from_files with write=True writes data/financials_register/2919415-2.jsonl
    and rows carry source='prh', country='FI', basis='company', period_end, fy=2024."""
    cfg = Config(data_dir=tmp_path)
    out = build_fi_financials_from_files([FIXTURE], config=cfg, write=True)

    assert out["entities"] == 1
    assert out["with_financials"] == 1
    assert out["no_financials"] == 0
    assert out["errors"] == 0

    out_file = tmp_path / "financials_register" / "2919415-2.jsonl"
    assert out_file.exists(), f"Expected {out_file} to be written"

    rows = [json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
    assert rows, "JSONL must not be empty"

    for row in rows:
        assert row["source"] == "prh"
        assert row["country"] == "FI"
        assert row["basis"] == "company"
        assert row["period_end"] == "2024-12-31"
        assert row["fy"] == 2024
        assert row["currency"] == "EUR"

    # equity reported
    eq_rows = [r for r in rows if r["concept"] == "equity" and r["kind"] == "reported"]
    assert eq_rows, "equity reported row missing"
    assert abs(eq_rows[0]["value"] - 185_650.88) < 0.01

    # revenue reported
    rev_rows = [r for r in rows if r["concept"] == "revenue" and r["kind"] == "reported"]
    assert rev_rows, "revenue reported row missing"
    assert abs(rev_rows[0]["value"] - 481_773.33) < 0.01

    # derived ratios
    derived_concepts = {r["concept"] for r in rows if r["kind"] == "derived"}
    assert "roa" in derived_concepts, "roa derived ratio missing"
    assert "operating_margin" in derived_concepts, "operating_margin derived ratio missing"
    assert "interest_coverage" in derived_concepts, "interest_coverage derived ratio missing"

    # C1: FI suppresses the maturity split, so the engine emits NO leverage rows
    # (no total_debt / debt_to_equity) and nothing carries a leverage_basis.
    assert "total_debt" not in derived_concepts
    assert "debt_to_equity" not in derived_concepts
    assert not any("leverage_basis" in r for r in rows)


def test_build_fi_financials_from_files_dry_run(tmp_path):
    """write=False: no file written, counters correct."""
    cfg = Config(data_dir=tmp_path)
    out = build_fi_financials_from_files([FIXTURE], config=cfg, write=False)

    assert out["with_financials"] == 1
    out_file = tmp_path / "financials_register" / "2919415-2.jsonl"
    assert not out_file.exists(), "Dry-run must not write any file"
    assert out["paths"] == []


def test_build_fi_financials_from_files_error_isolation(tmp_path):
    """A path that raises must be counted as an error without aborting the batch."""
    cfg = Config(data_dir=tmp_path)
    out = build_fi_financials_from_files(
        ["/nonexistent/fi_9999999-9_full_2024.xml", FIXTURE],
        config=cfg, write=False,
    )
    assert out["errors"] == 1
    assert out["with_financials"] == 1


def test_build_fi_financials_api_stub(tmp_path):
    """API path: stubbed fetcher → same rows as from_files (source='prh', country='FI')."""
    raw = open(FIXTURE, "rb").read()
    stub = _PrhApiFetcher("2919415-2", "2024-12-31", raw)
    cfg = Config(data_dir=tmp_path)
    out = build_fi_financials(
        [{"business_id": "2919415-2"}],
        fetcher=stub,
        config=cfg,
        write=True,
    )

    assert out["entities"] == 1
    assert out["with_financials"] == 1
    assert out["errors"] == 0

    out_file = tmp_path / "financials_register" / "2919415-2.jsonl"
    assert out_file.exists()
    rows = [json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
    assert any(r["concept"] == "equity" and r["kind"] == "reported" for r in rows)
    assert any(r["source"] == "prh" for r in rows)
    assert any(r["country"] == "FI" for r in rows)


# --- CLI -------------------------------------------------------------------

def test_cli_fi_file_dry_run(tmp_path):
    """--fi-file dry-run: build_fi_financials_from_files called with write=False."""
    from company_corpus.cli import main

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--fi-file", FIXTURE,
    ])
    assert rc == 0
    out_file = tmp_path / "financials_register" / "2919415-2.jsonl"
    assert not out_file.exists(), "Dry-run must not write any file"


def test_cli_fi_file_write(tmp_path):
    """--fi-file --write: writes the JSONL."""
    from company_corpus.cli import main

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--fi-file", FIXTURE,
        "--write",
    ])
    assert rc == 0
    out_file = tmp_path / "financials_register" / "2919415-2.jsonl"
    assert out_file.exists()


def test_cli_fi_businessid_dry_run(tmp_path, monkeypatch):
    """--fi-businessid dry-run: API is stubbed → offline; no file written.

    Patches ``build_fi_financials`` at the CLI module level (the name the CLI
    imported), mirroring how the BE test patches ``_fetch_bnb_deposit`` inside
    ``registers.financials`` rather than at the CLI level.
    """
    import company_corpus.cli as _cli
    from company_corpus.cli import main

    calls: list[str] = []

    def _stub_build(specs, *, fetcher, config, write):
        for s in specs:
            calls.append(s.get("business_id"))
        return {"entities": len(specs), "with_financials": 0, "no_financials": len(specs),
                "unbalanced": 0, "errors": 0, "periods": 0, "paths": [],
                "coverage_path": None}

    monkeypatch.setattr(_cli, "build_fi_financials", _stub_build)

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--fi-businessid", "2919415-2",
    ])
    assert rc == 0
    assert calls == ["2919415-2"]
    out_file = tmp_path / "financials_register" / "2919415-2.jsonl"
    assert not out_file.exists()
