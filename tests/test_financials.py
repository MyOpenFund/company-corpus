from __future__ import annotations

from datetime import date

import pytest

from company_corpus.financials import (
    LEVERAGE_BASIS_CONCEPTS,
    build_period_summaries,
    normalized_rows,
    render_summary_html,
    stamp_leverage_basis,
)
from tests.conftest import SAMPLE_FACTS


def _summaries(**kw):
    return build_period_summaries(SAMPLE_FACTS, company="Apple Inc.",
                                  company_current="Apple Inc.", **kw)


def test_groups_into_annual_and_quarterly_periods():
    s = _summaries()
    keys = {(x.fy, x.frequency) for x in s}
    assert keys == {(2023, "annual"), (2023, "quarterly")}


def test_quarterly_picks_three_month_duration():
    q3 = next(x for x in _summaries() if x.frequency == "quarterly")
    # 3-month revenue (89.5B), not the 9-month YTD (270B).
    assert q3.values["revenue"]["value"] == 89498000000


def test_annual_period_fields_and_publication_date():
    fy = next(x for x in _summaries() if x.frequency == "annual")
    assert fy.values["revenue"]["value"] == 383285000000
    assert fy.values["net_income"]["value"] == 96995000000
    assert fy.period_end == date(2023, 9, 30)
    # First report of the period defines the publication date (earliest filed).
    assert fy.publication_date == date(2023, 11, 1)
    assert fy.sec_form == "10-K"
    assert fy.accession == "acc-fy23"


def test_instant_restatement_latest_filed_wins():
    fy = next(x for x in _summaries() if x.frequency == "annual")
    # Assets was restated 2024-02-01 -> the later value is used, but the
    # publication date stays the original (earliest) filing.
    assert fy.values["assets"]["value"] == 352583000000
    assert fy.publication_date == date(2023, 11, 1)


def test_point_in_time_name_applied():
    s = build_period_summaries(SAMPLE_FACTS, company="Apple Inc.",
                               company_current="Apple Inc.",
                               name_for_date=lambda d: "Old Apple Computer Inc")
    assert all(x.company == "Old Apple Computer Inc" for x in s)


def test_since_year_filter():
    assert _summaries(since_year=2024) == []
    assert len(_summaries(since_year=2023)) == 2


def test_render_summary_html_has_metrics_and_pubdate():
    fy = next(x for x in _summaries() if x.frequency == "annual")
    html = render_summary_html(fy)
    assert "Revenue" in html and "383,285,000,000" in html
    assert "2023-11-01" in html  # publication date is surfaced
    assert "FY2023" in html


def test_normalized_rows():
    fy = next(x for x in _summaries() if x.frequency == "annual")
    rows = normalized_rows("0000320193", fy)
    # Canonical RowBase (ARCH-C1): SEC issuer id is entity_id (id_scheme "cik"),
    # country "US", source "sec".
    assert all(r["entity_id"] == "0000320193" and r["id_scheme"] == "cik"
               and r["country"] == "US" and r["source"] == "sec"
               and r["fy"] == 2023 for r in rows)
    rev = next(r for r in rows if r["concept"] == "revenue")
    assert rev["value"] == 383285000000 and rev["publication_date"] == "2023-11-01"
    assert rev["kind"] == "reported"
    # Derived metrics are emitted alongside reported ones, flagged by kind.
    debt = next(r for r in rows if r["concept"] == "total_debt")
    assert debt["kind"] == "derived" and debt["unit"] == "USD"


def test_derived_aggregates():
    fy = next(x for x in _summaries() if x.frequency == "annual")
    d = fy.derived
    # Total debt sums noncurrent + current portion + commercial paper.
    assert d["total_debt"]["value"] == 95281000000 + 9822000000 + 5985000000
    # EBITDA = operating income + D&A.
    assert d["ebitda"]["value"] == 114301000000 + 11519000000
    # Net debt = total debt - cash - short-term investments.
    assert d["net_debt"]["value"] == 111088000000 - 29965000000 - 31590000000
    # Free cash flow = CFO - capex.
    assert d["free_cash_flow"]["value"] == 110543000000 - 10959000000
    # Integer-valued aggregates stay exact ints (no spurious .0 in the JSONL).
    assert isinstance(d["total_debt"]["value"], int)
    assert isinstance(d["net_debt"]["value"], int)
    assert isinstance(d["ebitda"]["value"], int)


def test_derived_ratios():
    fy = next(x for x in _summaries() if x.frequency == "annual")
    d = fy.derived
    assert d["debt_to_equity"]["value"] == pytest.approx(111088000000 / 62146000000)
    assert d["debt_to_equity"]["unit"] == "x"
    assert d["net_debt_to_ebitda"]["value"] == pytest.approx(49533000000 / 125820000000)
    assert d["current_ratio"]["value"] == pytest.approx(143566000000 / 145308000000)
    assert d["ebitda_margin"]["value"] == pytest.approx(125820000000 / 383285000000 * 100)
    assert d["ebitda_margin"]["unit"] == "%"
    assert d["effective_tax_rate"]["value"] == pytest.approx(16741000000 / 113736000000 * 100)
    assert d["interest_coverage"]["value"] == pytest.approx(114301000000 / 3933000000)
    # Label must reflect the actual numerator (operating income), not EBIT.
    assert "EBIT" not in d["interest_coverage"]["label"]
    assert "income" in d["interest_coverage"]["label"]


def _full_inputs():
    # Minimal inputs that yield both stock/flow ratios and stock/stock + flow/flow ones.
    def v(x):
        return {"value": float(x), "unit": "USD", "label": ""}
    return {
        "revenue": v(100), "operating_income": v(20), "net_income": v(10),
        "equity": v(200), "assets": v(400), "cash": v(30),
        "long_term_debt": v(50), "dep_amort": v(5),
    }


def test_annual_only_ratios_suppressed_for_sub_annual_periods():
    # net_debt/EBITDA and asset turnover divide a balance-sheet stock by a flow,
    # so a quarterly value would be ~4x off -- they must not be emitted.
    from company_corpus.financials import compute_derived
    d = compute_derived(_full_inputs(), frequency="quarterly")
    assert "net_debt_to_ebitda" not in d
    assert "asset_turnover" not in d
    # Stock/stock and flow/flow ratios are still meaningful sub-annually.
    assert d["debt_to_equity"]["value"] == pytest.approx(50 / 200)
    assert d["ebitda_margin"]["value"] == pytest.approx(25 / 100 * 100)
    # Same gate applies to semi-annual periods.
    assert "net_debt_to_ebitda" not in compute_derived(_full_inputs(), frequency="semi-annual")
    # ROE/ROA are stock/flow too -> also annual-only now.
    assert "roe" not in d and "roa" not in d


def test_annual_only_ratios_present_for_annual_periods():
    from company_corpus.financials import compute_derived
    d = compute_derived(_full_inputs(), frequency="annual")
    assert d["net_debt_to_ebitda"]["value"] == pytest.approx((50 - 30) / (20 + 5))
    assert d["asset_turnover"]["value"] == pytest.approx(100 / 400)
    # Default frequency is annual, preserving the previous call signature behavior.
    assert "net_debt_to_ebitda" in compute_derived(_full_inputs())


def test_reporting_currency_defaults_and_ties():
    from company_corpus.financials import reporting_currency
    assert reporting_currency({}) is None
    # Tie between two currencies breaks towards USD.
    assert reporting_currency({"a": [{"unit": "USD"}], "b": [{"unit": "EUR"}]}) == "USD"
    # Non-monetary units are not currencies.
    assert reporting_currency({"a": [{"unit": "USD/shares"}, {"unit": "shares"}]}) is None


def test_currency_filter_ignores_convenience_translation():
    from company_corpus.financials import (
        build_period_summaries,
        flatten_points,
        reporting_currency,
    )

    def dur(val, filed):
        return {"start": "2022-01-01", "end": "2022-12-31", "val": val,
                "accn": "a", "fy": 2022, "fp": "FY", "form": "20-F", "filed": filed}

    facts = {"facts": {"us-gaap": {
        "Revenues": {"label": "Revenue", "units": {
            "EUR": [dur(1000, "2023-01-01")],
            "USD": [dur(1100, "2023-06-01")],  # later-filed convenience translation
        }},
        "OperatingIncomeLoss": {"label": "OI", "units": {"EUR": [dur(200, "2023-01-01")]}},
        "NetIncomeLoss": {"label": "NI", "units": {"EUR": [dur(150, "2023-01-01")]}},
    }}}
    # EUR dominates (3 facts vs 1), so it is the reporting currency.
    assert reporting_currency(flatten_points(facts)) == "EUR"
    fy = next(x for x in build_period_summaries(facts, company="X", company_current="X")
              if x.frequency == "annual")
    # The later USD value must NOT win over the primary EUR fact (no currency mix).
    assert fy.values["revenue"]["value"] == 1000
    assert fy.values["revenue"]["unit"] == "EUR"
    assert fy.currency == "EUR"
    # Margins stay currency-invariant (EUR/EUR): net margin = 150/1000 = 15%.
    assert fy.derived["net_margin"]["value"] == pytest.approx(15.0)
    assert fy.derived["net_margin"]["unit"] == "%"  # ratio: no currency


def test_derived_rows_carry_reporting_currency():
    # A USD issuer's derived monetary rows stay USD; ratios stay %/x.
    fy = next(x for x in _summaries() if x.frequency == "annual")
    assert fy.currency == "USD"
    assert fy.derived["total_debt"]["unit"] == "USD"
    assert fy.derived["net_debt_to_ebitda"]["unit"] == "x"
    # normalized_rows surface the currency explicitly on every row.
    rows = normalized_rows("0000320193", fy)
    assert all(r["currency"] == "USD" for r in rows)


def test_compute_derived_relabels_units_for_foreign_currency():
    from company_corpus.financials import compute_derived
    vals = {
        "revenue": {"value": 100.0, "unit": "EUR", "label": ""},
        "operating_income": {"value": 20.0, "unit": "EUR", "label": ""},
        "equity": {"value": 200.0, "unit": "EUR", "label": ""},
        "long_term_debt": {"value": 50.0, "unit": "EUR", "label": ""},
        "shares_outstanding": {"value": 10.0, "unit": "shares", "label": ""},
    }
    d = compute_derived(vals, currency="EUR")
    assert d["total_debt"]["unit"] == "EUR"            # monetary -> currency
    assert d["book_value_per_share"]["unit"] == "EUR/shares"  # per-share -> ccy/shares
    assert d["debt_to_equity"]["unit"] == "x"          # ratio unchanged


def test_derived_omits_metrics_with_missing_inputs():
    # A bare period with no debt/EBITDA inputs yields no leverage metrics.
    from company_corpus.financials import compute_derived
    d = compute_derived({"revenue": {"value": 100.0, "unit": "USD", "label": "Revenue"}})
    assert "total_debt" not in d and "ebitda" not in d and "net_debt_to_ebitda" not in d


def test_derived_rendered_in_html():
    fy = next(x for x in _summaries() if x.frequency == "annual")
    html = render_summary_html(fy)
    assert "Derived metrics" in html
    assert "EBITDA" in html and "Net debt / EBITDA" in html
    assert "Total debt" in html


def test_values_carry_source_tag():
    fy = next(x for x in _summaries() if x.frequency == "annual")
    # The resolved XBRL element backing each curated value is recorded.
    assert fy.values["revenue"]["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert fy.values["long_term_debt"]["tag"] == "LongTermDebtNoncurrent"


def test_normalized_reported_rows_carry_tag():
    fy = next(x for x in _summaries() if x.frequency == "annual")
    rows = normalized_rows("0000320193", fy)
    rev = next(r for r in rows if r["concept"] == "revenue" and r["kind"] == "reported")
    assert rev["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"


def test_total_debt_no_double_count_for_longtermdebt_rollup():
    from company_corpus.financials import compute_derived
    # LongTermDebt is the FASB roll-up (incl. current portion); adding the
    # current portion again would double-count -> must not happen.
    vals = {
        "long_term_debt": {"value": 100.0, "unit": "USD", "tag": "LongTermDebt"},
        "lt_debt_current": {"value": 30.0, "unit": "USD", "tag": "LongTermDebtCurrent"},
    }
    d = compute_derived(vals)
    assert d["total_debt"]["value"] == 100  # not 130


def test_total_debt_no_double_count_for_debtcurrent():
    from company_corpus.financials import compute_derived
    # DebtCurrent already includes current maturities of LTD (= lt_debt_current).
    vals = {
        "long_term_debt": {"value": 100.0, "unit": "USD", "tag": "LongTermDebtNoncurrent"},
        "lt_debt_current": {"value": 30.0, "unit": "USD", "tag": "LongTermDebtCurrent"},
        "short_term_debt": {"value": 40.0, "unit": "USD", "tag": "DebtCurrent"},
    }
    d = compute_derived(vals)
    assert d["total_debt"]["value"] == 140  # 100 + 40 (DebtCurrent), current portion not re-added


def test_total_debt_no_double_count_for_ifrs_borrowings_rollup():
    from company_corpus.financials import compute_derived
    # IFRS `Borrowings` is a current-INCLUSIVE roll-up; `CurrentBorrowings` is its
    # own current tranche (already inside it). With no NoncurrentBorrowings the
    # true total is the roll-up itself (1000), NOT 1000 + 300.
    vals = {
        "long_term_debt": {"value": 1000.0, "unit": "EUR", "tag": "Borrowings"},
        "short_term_debt": {"value": 300.0, "unit": "EUR", "tag": "CurrentBorrowings"},
        "equity": {"value": 2000.0, "unit": "EUR"},
    }
    d = compute_derived(vals, currency="EUR")
    assert d["total_debt"]["value"] == 1000            # not 1300
    assert d["debt_to_equity"]["value"] == pytest.approx(0.50)  # not 0.65


def test_total_debt_ifrs_clean_split_unchanged():
    from company_corpus.financials import compute_derived
    # Clean IFRS split: NoncurrentBorrowings (long-term only) + CurrentBorrowings
    # -> additive, total = 700 + 300 = 1000 (the roll-up guard must not fire here).
    vals = {
        "long_term_debt": {"value": 700.0, "unit": "EUR", "tag": "NoncurrentBorrowings"},
        "short_term_debt": {"value": 300.0, "unit": "EUR", "tag": "CurrentBorrowings"},
    }
    d = compute_derived(vals, currency="EUR")
    assert d["total_debt"]["value"] == 1000


def test_total_debt_us_gaap_longtermdebt_plus_debtcurrent_not_doubled():
    from company_corpus.financials import compute_derived
    # US-GAAP `LongTermDebt` roll-up (incl. current maturities) + `DebtCurrent`
    # (the total current-debt line it already subsumes) -> total is the roll-up
    # (1000), not 1000 + 300.
    vals = {
        "long_term_debt": {"value": 1000.0, "unit": "USD", "tag": "LongTermDebt"},
        "short_term_debt": {"value": 300.0, "unit": "USD", "tag": "DebtCurrent"},
    }
    d = compute_derived(vals)
    assert d["total_debt"]["value"] == 1000            # not 1300


def test_total_debt_rollup_still_adds_separate_short_term_borrowing():
    from company_corpus.financials import compute_derived
    # A LongTermDebt roll-up + genuinely-separate commercial paper (not the
    # roll-up's own current tranche) -> the CP IS additive: 1000 + 200 = 1200.
    vals = {
        "long_term_debt": {"value": 1000.0, "unit": "USD", "tag": "LongTermDebt"},
        "short_term_debt": {"value": 200.0, "unit": "USD", "tag": "CommercialPaper"},
    }
    d = compute_derived(vals)
    assert d["total_debt"]["value"] == 1200


def test_total_debt_short_term_only_borrower():
    from company_corpus.financials import compute_derived
    # A commercial-paper / all-current-debt issuer (only short_term_debt, no
    # long-term line) still gets a real total_debt = short_term_debt and the
    # leverage ratios that build on it.
    vals = {
        "short_term_debt": {"value": 500.0, "unit": "USD", "tag": "CommercialPaper"},
        "equity": {"value": 1000.0, "unit": "USD"},
        "assets": {"value": 2000.0, "unit": "USD"},
    }
    d = compute_derived(vals)
    assert d["total_debt"]["value"] == 500
    assert d["debt_to_equity"]["value"] == pytest.approx(0.5)
    assert d["debt_to_assets"]["value"] == pytest.approx(0.25)


def test_total_debt_none_for_debt_free_filer():
    from company_corpus.financials import compute_derived
    # No debt component of any kind -> total_debt stays None (never coerced to 0).
    vals = {"equity": {"value": 1000.0, "unit": "USD"}, "assets": {"value": 2000.0, "unit": "USD"}}
    d = compute_derived(vals)
    assert "total_debt" not in d
    assert "debt_to_equity" not in d


def test_net_debt_no_double_count_for_combined_cash_tag():
    from company_corpus.financials import compute_derived
    vals = {
        "long_term_debt": {"value": 100.0, "unit": "USD", "tag": "LongTermDebtNoncurrent"},
        "cash": {"value": 60.0, "unit": "USD", "tag": "CashCashEquivalentsAndShortTermInvestments"},
        "short_term_investments": {"value": 25.0, "unit": "USD", "tag": "ShortTermInvestments"},
    }
    d = compute_derived(vals)
    # cash already includes STI -> do NOT subtract STI again: 100 - 60 = 40
    assert d["net_debt"]["value"] == 40
    # cash_ratio numerator also must not re-add STI (here lc absent -> ratio omitted)
    assert "cash_ratio" not in d


def test_net_debt_excludes_generic_long_term_investments():
    from company_corpus.financials import compute_derived
    # The generic us-gaap:LongTermInvestments tag can hold illiquid equity-method
    # / strategic stakes -> it must NOT be netted against debt (a levered
    # industrial would otherwise read net_debt ~ 0).
    vals = {
        "long_term_debt": {"value": 1000.0, "unit": "USD", "tag": "LongTermDebtNoncurrent"},
        "cash": {"value": 100.0, "unit": "USD", "tag": "CashAndCashEquivalentsAtCarryingValue"},
        "long_term_investments": {"value": 900.0, "unit": "USD", "tag": "LongTermInvestments"},
    }
    d = compute_derived(vals)
    assert d["net_debt"]["value"] == 900          # 1000 - 100 - 0, NOT 0


def test_net_debt_offsets_marketable_long_term_securities():
    from company_corpus.financials import compute_derived
    # Genuinely-marketable long-term securities (MarketableSecuritiesNoncurrent) ARE
    # a liquid offset to debt (cash-rich issuers like Apple/Microsoft).
    vals = {
        "long_term_debt": {"value": 1000.0, "unit": "USD", "tag": "LongTermDebtNoncurrent"},
        "cash": {"value": 100.0, "unit": "USD", "tag": "CashAndCashEquivalentsAtCarryingValue"},
        "long_term_investments": {"value": 900.0, "unit": "USD", "tag": "MarketableSecuritiesNoncurrent"},
    }
    d = compute_derived(vals)
    assert d["net_debt"]["value"] == 0            # 1000 - 100 - 900


def test_negative_equity_suppresses_roe_and_dte():
    from company_corpus.financials import compute_derived
    vals = {
        "net_income": {"value": 10.0, "unit": "USD"},
        "equity": {"value": -50.0, "unit": "USD"},
        "long_term_debt": {"value": 100.0, "unit": "USD", "tag": "LongTermDebtNoncurrent"},
        "assets": {"value": 400.0, "unit": "USD"},
    }
    d = compute_derived(vals)  # annual
    assert "roe" not in d and "debt_to_equity" not in d
    assert d["roa"]["value"] == pytest.approx(10 / 400 * 100)  # roa fine (assets > 0)


def test_nonpositive_pretax_suppresses_effective_tax_rate():
    from company_corpus.financials import compute_derived
    vals = {"income_tax": {"value": 5.0, "unit": "USD"},
            "pretax_income": {"value": -20.0, "unit": "USD"}}
    assert "effective_tax_rate" not in compute_derived(vals)


def test_roe_gated_on_mixed_parent_vs_consolidated_nci_base():
    from company_corpus.financials import compute_derived
    # net_income = parent-attributable (ProfitLossAttributableToOwnersOfParent) but
    # equity = the consolidated `Equity` total (incl. NCI), with MATERIAL NCI ->
    # ROE would divide a parent numerator by a consolidated equity base (mixed,
    # wrong). Gate ROE + per-common-share book value; roa (assets base) is fine.
    vals = {
        "net_income": {"value": 100.0, "unit": "EUR", "tag": "ProfitLossAttributableToOwnersOfParent"},
        "equity": {"value": 1000.0, "unit": "EUR", "tag": "Equity"},
        "noncontrolling_interest": {"value": 200.0, "unit": "EUR", "tag": "NoncontrollingInterests"},
        "assets": {"value": 3000.0, "unit": "EUR"},
        "shares_outstanding": {"value": 100.0, "unit": "shares"},
    }
    d = compute_derived(vals, currency="EUR")  # annual
    assert "roe" not in d                       # mixed base -> gated
    assert "book_value_per_share" not in d      # equity-denominated -> gated
    assert d["roa"]["value"] == pytest.approx(100 / 3000 * 100)  # unaffected


def test_roe_not_gated_without_material_nci_dk_esef_style():
    from company_corpus.financials import compute_derived
    # DK-ESEF style: equity resolves to the `Equity` total tag but there is NO NCI,
    # so `Equity` IS parent equity. Parent-attributable NI. No mix -> ROE unchanged.
    vals = {
        "net_income": {"value": 100.0, "unit": "EUR", "tag": "ProfitLossAttributableToOwnersOfParent"},
        "equity": {"value": 1000.0, "unit": "EUR", "tag": "Equity"},
        "assets": {"value": 3000.0, "unit": "EUR"},
    }
    d = compute_derived(vals, currency="EUR")
    assert d["roe"]["value"] == pytest.approx(10.0)  # 100 / 1000, unchanged


def test_roe_not_gated_when_both_bases_consolidated():
    from company_corpus.financials import compute_derived
    # Both sides consolidated (equity=Equity total, net_income=ProfitLoss total) with
    # material NCI -> a consistent consolidated ROE, not a mix -> emitted, not gated.
    vals = {
        "net_income": {"value": 120.0, "unit": "EUR", "tag": "ProfitLoss"},
        "equity": {"value": 1000.0, "unit": "EUR", "tag": "Equity"},
        "noncontrolling_interest": {"value": 200.0, "unit": "EUR", "tag": "NoncontrollingInterests"},
    }
    d = compute_derived(vals, currency="EUR")
    assert d["roe"]["value"] == pytest.approx(12.0)  # 120 / 1000, consolidated


def test_roe_not_gated_when_nci_immaterial():
    from company_corpus.financials import compute_derived
    # Mixed tags but NCI is 0.5% of equity (< 1% materiality) -> the base mix moves
    # ROE by <1%, so we keep the number rather than drop good data.
    vals = {
        "net_income": {"value": 100.0, "unit": "EUR", "tag": "ProfitLossAttributableToOwnersOfParent"},
        "equity": {"value": 1000.0, "unit": "EUR", "tag": "Equity"},
        "noncontrolling_interest": {"value": 5.0, "unit": "EUR", "tag": "NoncontrollingInterests"},
    }
    d = compute_derived(vals, currency="EUR")
    assert d["roe"]["value"] == pytest.approx(10.0)  # immaterial NCI -> not gated


def test_roe_roa_are_annual_only():
    from company_corpus.financials import compute_derived
    vals = {"net_income": {"value": 10.0, "unit": "USD"},
            "equity": {"value": 200.0, "unit": "USD"},
            "assets": {"value": 400.0, "unit": "USD"}}
    q = compute_derived(vals, frequency="quarterly")
    assert "roe" not in q and "roa" not in q
    a = compute_derived(vals, frequency="annual")
    assert a["roe"]["value"] == pytest.approx(5.0) and a["roa"]["value"] == pytest.approx(2.5)


def test_dep_amort_no_bare_depreciation_fallback():
    from company_corpus.financials import CONCEPTS_BY_KEY
    assert "Depreciation" not in CONCEPTS_BY_KEY["dep_amort"].tags


def test_is_financial_classifies_sic_ranges():
    from company_corpus.financials import _is_financial
    assert _is_financial("6311") is True   # insurer
    assert _is_financial("6022") is True    # state bank
    assert _is_financial("3571") is False   # electronic computers (Apple)
    assert _is_financial(None) is False
    assert _is_financial("6500") is False   # real estate left non-financial


def _sector_vals():
    return {
        "revenue": {"value": 100.0, "unit": "USD"},
        "operating_income": {"value": 20.0, "unit": "USD"},
        "net_income": {"value": 10.0, "unit": "USD"},
        "equity": {"value": 200.0, "unit": "USD"},
        "assets": {"value": 400.0, "unit": "USD"},
        "dep_amort": {"value": 5.0, "unit": "USD"},
        "assets_current": {"value": 150.0, "unit": "USD"},
        "liabilities_current": {"value": 80.0, "unit": "USD"},
        "long_term_debt": {"value": 50.0, "unit": "USD", "tag": "LongTermDebtNoncurrent"},
        "gross_profit": {"value": 40.0, "unit": "USD"},
        "cash": {"value": 30.0, "unit": "USD", "tag": "CashAndCashEquivalentsAtCarryingValue"},
        "interest_expense": {"value": 5.0, "unit": "USD"},
    }


def test_financial_metrics_flagged_not_dropped():
    from company_corpus.financials import compute_derived
    d = compute_derived(_sector_vals(), frequency="annual", is_financial=True)
    # Nothing is dropped -- sector-sensitive metrics are still present...
    for k in ("ebitda", "ebitda_margin", "current_ratio", "quick_ratio",
              "working_capital", "asset_turnover", "gross_margin",
              "interest_coverage", "net_debt"):
        assert k in d, k
        assert d[k]["sector_relevant"] is False, k
    # ...sector-neutral metrics are flagged relevant.
    assert d["net_margin"]["sector_relevant"] is True
    assert d["roe"]["sector_relevant"] is True
    assert d["total_debt"]["sector_relevant"] is True


def test_non_financial_everything_sector_relevant():
    from company_corpus.financials import compute_derived
    d = compute_derived(_sector_vals(), frequency="annual", is_financial=False)
    assert all(v["sector_relevant"] is True for v in d.values())


def test_unknown_sector_does_not_assert_sector_relevance():
    from company_corpus.financials import compute_derived
    # is_financial=None (sector UNKNOWN, e.g. the EU/ESEF pillar) must NOT stamp the
    # bank/insurer-sensitive metrics sector_relevant=True -- that asserts a relevance
    # we can't back. They carry None (unknown); sector-neutral metrics stay True.
    d = compute_derived(_sector_vals(), frequency="annual", is_financial=None)
    for k in ("ebitda", "interest_coverage", "current_ratio", "net_debt", "gross_margin"):
        assert d[k]["sector_relevant"] is None, k        # not True
    assert d["net_margin"]["sector_relevant"] is True    # sector-neutral -> still True
    assert d["total_debt"]["sector_relevant"] is True


def test_period_summary_unknown_sector_yields_is_financial_none():
    from company_corpus.financials import summaries_from_flat
    from company_corpus.xbrl import IFRS_CONCEPTS
    # An EU-style summary (sector_known=False) -> is_financial None, and its
    # sector-sensitive derived metrics are not falsely stamped sector_relevant=True.
    flat = {
        "Revenue": [{"val": 100.0, "start": "2020-01-01", "end": "2020-12-31",
                     "unit": "EUR", "tag": "Revenue", "filed": "2021-04-01", "form": "ar"}],
        "ProfitLossFromOperatingActivities": [{"val": 20.0, "start": "2020-01-01", "end": "2020-12-31",
                     "unit": "EUR", "tag": "ProfitLossFromOperatingActivities", "filed": "2021-04-01", "form": "ar"}],
        "DepreciationAndAmortisationExpense": [{"val": 5.0, "start": "2020-01-01", "end": "2020-12-31",
                     "unit": "EUR", "tag": "DepreciationAndAmortisationExpense", "filed": "2021-04-01", "form": "ar"}],
    }
    s = summaries_from_flat(flat, concepts=IFRS_CONCEPTS, company="X",
                            company_current="X", sic=None, sector_known=False)[0]
    assert s.is_financial is None
    assert s.derived["ebitda"]["sector_relevant"] is None   # unknown, not True


def test_period_summary_is_financial_from_sic():
    s = build_period_summaries(SAMPLE_FACTS, company="X", company_current="X", sic="6311")
    assert all(x.is_financial for x in s)
    assert all(x.sic == "6311" for x in s)


def test_normalized_rows_carry_sic_and_sector_flags():
    # Insurer SIC -> is_financial True on every row; sector-sensitive derived rows
    # flagged, sector-neutral ones relevant. Nothing is dropped.
    s = build_period_summaries(SAMPLE_FACTS, company="X", company_current="X", sic="6311")
    fy = next(x for x in s if x.frequency == "annual")
    rows = normalized_rows("0000320193", fy)
    assert all(r["sic"] == "6311" for r in rows)
    assert all(r["is_financial"] is True for r in rows)
    ebitda = next(r for r in rows if r["concept"] == "ebitda" and r["kind"] == "derived")
    assert ebitda["sector_relevant"] is False
    net_margin = next(r for r in rows if r["concept"] == "net_margin")
    assert net_margin["sector_relevant"] is True


def test_edgar_xbrl_threads_sic(xbrl_fetcher, config):
    from company_corpus.sources.edgar_xbrl import EdgarXBRL
    src = EdgarXBRL(fetcher=xbrl_fetcher, config=config)
    _facts, summaries = src.period_summaries("0000320193")
    assert summaries and all(s.sic == "3571" for s in summaries)


def test_edgar_xbrl_attaches_ttm_block(xbrl_fetcher, config):
    from company_corpus.sources.edgar_xbrl import EdgarXBRL
    src = EdgarXBRL(fetcher=xbrl_fetcher, config=config)
    _facts, summaries = src.period_summaries("0000320193")
    fy = next(s for s in summaries if s.frequency == "annual")
    # TTM container is always populated (dict), even if metrics needing a prior
    # year are absent in this single-year fixture.
    assert isinstance(fy.ttm, dict)
    # Margin-style TTM metrics need only the FY flow window -> present.
    assert "net_margin_ttm" in fy.ttm


# ---------------------------------------------------------------------------
# C1 — leverage-basis stamping (shared engine helper)
# ---------------------------------------------------------------------------

def _lev_rows(basis_field=False):
    """A representative row list: reported + the four leverage derived rows +
    a non-leverage derived row."""
    return [
        {"kind": "reported", "concept": "long_term_debt", "value": 350},
        {"kind": "derived", "concept": "total_debt", "value": 600},
        {"kind": "derived", "concept": "debt_to_equity", "value": 1.5},
        {"kind": "derived", "concept": "net_debt", "value": 500},
        {"kind": "derived", "concept": "debt_to_assets", "value": 0.6},
        {"kind": "derived", "concept": "current_ratio", "value": 2.0},
    ]


def test_stamp_leverage_basis_stamps_only_the_four_leverage_rows():
    rows = _lev_rows()
    ret = stamp_leverage_basis(rows, "borrowings")
    assert ret is rows                                   # returns the same list
    assert LEVERAGE_BASIS_CONCEPTS == {
        "total_debt", "debt_to_equity", "net_debt", "debt_to_assets"}
    for r in rows:
        if r["kind"] == "derived" and r["concept"] in LEVERAGE_BASIS_CONCEPTS:
            assert r["leverage_basis"] == "borrowings"
        else:                                            # reported + non-leverage derived
            assert "leverage_basis" not in r


def test_stamp_leverage_basis_none_is_a_noop():
    """Backward-compat: None (SEC / EU-ESEF pillar) leaves every row untouched."""
    rows = _lev_rows()
    stamp_leverage_basis(rows, None)
    assert not any("leverage_basis" in r for r in rows)


def test_stamp_leverage_basis_accepts_total_liabilities():
    rows = _lev_rows()
    stamp_leverage_basis(rows, "total_liabilities")
    d2e = next(r for r in rows if r["concept"] == "debt_to_equity")
    assert d2e["leverage_basis"] == "total_liabilities"
