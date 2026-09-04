"""Tier 1-3 concept/metric additions: exact-arithmetic unit tests."""
from __future__ import annotations

import pytest

from company_corpus.financials import CONCEPTS_BY_KEY, compute_derived


def _v(x, unit="USD", tag=None):
    d = {"value": float(x), "unit": unit}
    if tag:
        d["tag"] = tag
    return d


def _inputs():
    return {
        "revenue": _v(100), "operating_income": _v(20), "net_income": _v(10),
        "equity": _v(200), "assets": _v(400),
        "cash": _v(30, tag="CashAndCashEquivalentsAtCarryingValue"),
        "long_term_debt": _v(50, tag="LongTermDebtNoncurrent"),
        "long_term_investments": _v(40), "income_tax": _v(5), "pretax_income": _v(15),
        "cfo": _v(25), "capex": _v(5), "receivables": _v(10), "inventory": _v(8),
        "payables": _v(6), "cost_of_revenue": _v(60), "shares_outstanding": _v(10, "shares"),
        "preferred_stock": _v(20), "dividends_paid": _v(4), "buybacks": _v(3),
    }


def test_net_debt_subtracts_long_term_investments():
    d = compute_derived(_inputs())
    # 50 debt - 30 cash - 0 STI - 40 LT investments = -20 (net cash)
    assert d["net_debt"]["value"] == -20
    assert d["net_cash"]["value"] == 20


def test_roic_and_invested_capital():
    d = compute_derived(_inputs())
    # NOPAT = 20 * (1 - 5/15) = 13.3333 ; invested = 50 debt + 200 equity + 0 NCI = 250
    assert d["nopat"]["value"] == pytest.approx(13.3333, abs=1e-3)
    assert d["invested_capital"]["value"] == 250
    assert d["roic"]["value"] == pytest.approx(13.3333 / 250 * 100, abs=1e-3)


def test_book_value_nets_out_preferred():
    d = compute_derived(_inputs())
    # common equity = 200 - 20 preferred = 180
    assert d["book_value_per_share"]["value"] == pytest.approx(18.0)
    assert d["tangible_book_value"]["value"] == 180  # no goodwill/intangibles here
    assert d["tangible_book_value_per_share"]["value"] == pytest.approx(18.0)


def test_roe_uses_net_income_to_common():
    vals = _inputs()
    vals["preferred_dividends"] = _v(2)
    d = compute_derived(vals)
    # (10 - 2 preferred dividends) / 200 = 4%
    assert d["roe"]["value"] == pytest.approx(4.0)


def test_working_capital_cycle_days():
    d = compute_derived(_inputs())
    assert d["dso"]["value"] == pytest.approx(10 / (100 / 365), abs=1e-3)
    assert d["dio"]["value"] == pytest.approx(8 / (60 / 365), abs=1e-3)
    assert d["dpo"]["value"] == pytest.approx(6 / (60 / 365), abs=1e-3)
    assert d["ccc"]["value"] == pytest.approx(d["dso"]["value"] + d["dio"]["value"] - d["dpo"]["value"])
    assert d["ccc"]["unit"] == "days"


def test_payout_and_cash_conversion():
    d = compute_derived(_inputs())
    assert d["dividend_payout"]["value"] == pytest.approx(40.0)        # 4/10
    assert d["total_payout"]["value"] == pytest.approx((4 + 3) / 20 * 100)  # (div+buyback)/FCF
    assert d["cash_conversion"]["value"] == pytest.approx(200.0)       # FCF 20 / NI 10
    assert d["capex_intensity"]["value"] == pytest.approx(5.0)
    assert d["cfo_to_debt"]["value"] == pytest.approx(0.5)
    assert d["fcf_to_debt"]["value"] == pytest.approx(0.4)


def test_cycle_metrics_annual_only():
    d = compute_derived(_inputs(), frequency="quarterly")
    for k in ("dso", "dio", "dpo", "ccc", "roic", "cfo_to_debt", "fcf_to_debt"):
        assert k not in d


def test_new_metrics_flagged_for_financials_not_dropped():
    d = compute_derived(_inputs(), is_financial=True)
    for k in ("net_cash", "roic", "ccc", "cfo_to_debt", "capex_intensity"):
        assert k in d and d[k]["sector_relevant"] is False
    # sector-neutral additions stay relevant
    assert d["dividend_payout"]["sector_relevant"] is True


def test_preferred_concept_prioritises_carrying_value():
    # Verified empirically: PreferredStockValue is par/0 for many filers; the
    # incl.-APIC carrying tag must take priority.
    assert CONCEPTS_BY_KEY["preferred_stock"].tags[0] == "PreferredStockIncludingAdditionalPaidInCapital"
    assert CONCEPTS_BY_KEY["preferred_stock"].tags[-1] == "PreferredStockValue"


def test_equity_is_parent_only():
    assert CONCEPTS_BY_KEY["equity"].tags == ("StockholdersEquity",)
    assert "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest" \
        in CONCEPTS_BY_KEY["equity_total"].tags


def test_pretax_reconstructed_from_geographic_split():
    # No consolidated pretax line; only Domestic + Foreign (e.g. McDonald's).
    vals = {"operating_income": _v(20), "income_tax": _v(2.1),
            "pretax_domestic": _v(3), "pretax_foreign": _v(7),
            "long_term_debt": _v(50, tag="LongTermDebtNoncurrent"), "equity": _v(200)}
    d = compute_derived(vals)
    assert d["effective_tax_rate"]["value"] == pytest.approx(21.0)   # 2.1 / (3+7)
    assert d["nopat"]["value"] == pytest.approx(20 * (1 - 0.21), abs=1e-6)
    assert d["roic"]["value"] == pytest.approx(20 * 0.79 / 250 * 100, abs=1e-6)


def test_gross_margin_derived_from_cost_when_untagged():
    vals = {"revenue": _v(100), "cost_of_revenue": _v(60)}  # no GrossProfit line
    assert compute_derived(vals)["gross_margin"]["value"] == pytest.approx(40.0)
    # An explicit GrossProfit tag still takes precedence.
    vals["gross_profit"] = _v(45)
    assert compute_derived(vals)["gross_margin"]["value"] == pytest.approx(45.0)


def test_capital_lease_debt_fallback_resolves_total_debt():
    assert "LongTermDebtAndCapitalLeaseObligations" in CONCEPTS_BY_KEY["long_term_debt"].tags
    vals = {"long_term_debt": _v(90, tag="LongTermDebtAndCapitalLeaseObligations")}
    # Treated as the noncurrent portion -> no roll-up overlap; total debt = 90.
    assert compute_derived(vals)["total_debt"]["value"] == 90


def test_period_resolution_prefers_higher_priority_tag_per_period():
    # A filer that switched cost-of-revenue tags across years: the primary tag is
    # stale (2022 only), the recent year is under the second fallback. Per-period
    # resolution must surface the recent year from the second tag (not drop it).
    from company_corpus.financials import build_period_summaries

    def dur(tag_val, start, end, filed):
        return {"start": start, "end": end, "val": tag_val, "accn": "a",
                "fy": 2023, "fp": "FY", "form": "10-K", "filed": filed}

    facts = {"facts": {"us-gaap": {
        "CostOfRevenue": {"label": "COGS", "units": {"USD": [
            dur(100, "2021-01-01", "2021-12-31", "2022-02-01")]}},          # stale primary
        "CostOfGoodsAndServicesSold": {"label": "COGS", "units": {"USD": [
            dur(140, "2023-01-01", "2023-12-31", "2024-02-01")]}},          # recent, 2nd fallback
        "Revenues": {"label": "Rev", "units": {"USD": [
            dur(500, "2023-01-01", "2023-12-31", "2024-02-01")]}},
    }}}
    s = build_period_summaries(facts, company="X", company_current="X")
    fy23 = next(x for x in s if x.fy == 2023)
    assert fy23.values["cost_of_revenue"]["value"] == 140
    assert fy23.values["cost_of_revenue"]["tag"] == "CostOfGoodsAndServicesSold"
