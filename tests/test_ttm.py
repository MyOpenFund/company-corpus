from __future__ import annotations

from datetime import date

import pytest

from company_corpus.financials import (
    _build_flow_series,
    _standalone_quarter,
    _ttm_flow,
    flatten_points,
)


def _dur(start, end, val, filed, fp, form):
    return {"start": start, "end": end, "val": val, "accn": "a",
            "fy": 2025, "fp": fp, "form": form, "filed": filed}


# Apple net income: 3-month standalone quarters + the FY25 annual + the 9-month
# YTD ending 2025-06-28 (so the unreported FY-end quarter can be derived).
NI_FACTS = {"facts": {"us-gaap": {"NetIncomeLoss": {"label": "NI", "units": {"USD": [
    _dur("2024-09-29", "2024-12-28", 36330000000, "2025-01-30", "Q1", "10-Q"),
    _dur("2024-12-29", "2025-03-29", 24780000000, "2025-05-01", "Q2", "10-Q"),
    _dur("2025-03-30", "2025-06-28", 23434000000, "2025-08-01", "Q3", "10-Q"),
    _dur("2024-09-29", "2025-06-28", 84544000000, "2025-08-01", "Q3", "10-Q"),  # 9M YTD
    _dur("2024-09-29", "2025-09-27", 112010000000, "2025-11-01", "FY", "10-K"),  # FY25
    _dur("2025-09-28", "2025-12-27", 42097000000, "2026-01-29", "Q1", "10-Q"),
    _dur("2025-12-28", "2026-03-28", 29578000000, "2026-05-01", "Q2", "10-Q"),
]}}}}}


def test_build_flow_series_buckets_durations():
    s = _build_flow_series(flatten_points(NI_FACTS), "USD")
    assert s.quarterly["net_income"][date(2025, 12, 27)] == 42097000000
    assert s.annual["net_income"][date(2025, 9, 27)] == 112010000000
    assert s.ytd9["net_income"][date(2025, 6, 28)] == 84544000000


def test_standalone_quarter_derives_unreported_q4():
    s = _build_flow_series(flatten_points(NI_FACTS), "USD")
    # Direct 3-month quarter.
    assert _standalone_quarter(s, "net_income", date(2025, 12, 27)) == 42097000000
    # Fiscal year-end quarter is not reported standalone -> FY - 9M YTD.
    assert _standalone_quarter(s, "net_income", date(2025, 9, 27)) == 112010000000 - 84544000000


def test_ttm_flow_sums_trailing_four_quarters():
    s = _build_flow_series(flatten_points(NI_FACTS), "USD")
    # TTM net income to 2025-12-27 = Q(Dec25)+Q(Sep25 derived)+Q(Jun25)+Q(Mar25)
    ttm = _ttm_flow(s, "net_income", date(2025, 12, 27), "quarterly")
    assert ttm == 42097000000 + (112010000000 - 84544000000) + 23434000000 + 24780000000
    assert ttm == 117777000000


def test_ttm_flow_annual_uses_fy_value():
    s = _build_flow_series(flatten_points(NI_FACTS), "USD")
    assert _ttm_flow(s, "net_income", date(2025, 9, 27), "annual") == 112010000000


def test_compute_ttm_derived_roa_uses_average_assets():
    from company_corpus.financials import compute_ttm_derived
    # AAPL @ 2025-12-27: TTM NI 117,777M; avg assets (379,297 + 344,085)/2.
    d = compute_ttm_derived(
        t12={"net_income": 117777e6, "revenue": 400000e6},
        avg_assets=(379297e6 + 344085e6) / 2,
        avg_equity=None, pit_net_debt=None,
    )
    assert d["roa_ttm"]["value"] == pytest.approx(32.5629, abs=1e-3)
    assert d["roa_ttm"]["unit"] == "%"


def test_compute_ttm_derived_flags_financials_not_dropped():
    from company_corpus.financials import compute_ttm_derived
    d = compute_ttm_derived(
        t12={"revenue": 100e6, "operating_income": 20e6, "dep_amort": 5e6,
             "interest_expense": 2e6, "gross_profit": 40e6},
        avg_assets=400e6, avg_equity=200e6, pit_net_debt=10e6, is_financial=True,
    )
    for k in ("ebitda_margin_ttm", "interest_coverage_ttm", "asset_turnover_ttm",
              "gross_margin_ttm", "net_debt_to_ebitda_ttm"):
        assert k in d and d[k]["sector_relevant"] is False
    assert d["operating_margin_ttm"]["value"] == pytest.approx(20.0)
    assert d["operating_margin_ttm"]["sector_relevant"] is True


def test_compute_ttm_derived_omits_metrics_with_missing_inputs():
    from company_corpus.financials import compute_ttm_derived
    d = compute_ttm_derived(t12={"revenue": None, "net_income": 10e6},
                            avg_assets=None, avg_equity=None, pit_net_debt=None)
    assert "net_margin_ttm" not in d   # revenue missing
    assert "roa_ttm" not in d          # avg_assets missing


def _instant(end, val, filed):
    return {"end": end, "val": val, "accn": "a", "fy": 2025, "fp": "Q",
            "form": "10-Q", "filed": filed}


AAPL_TTM_FACTS = {"facts": {"us-gaap": {
    "NetIncomeLoss": {"label": "NI", "units": {"USD": [
        _dur("2023-10-01", "2023-12-30", 33916000000, "2024-02-02", "Q1", "10-Q"),
        _dur("2024-09-29", "2024-12-28", 36330000000, "2025-01-30", "Q1", "10-Q"),
        _dur("2024-12-29", "2025-03-29", 24780000000, "2025-05-01", "Q2", "10-Q"),
        _dur("2025-03-30", "2025-06-28", 23434000000, "2025-08-01", "Q3", "10-Q"),
        _dur("2024-09-29", "2025-06-28", 84544000000, "2025-08-01", "Q3", "10-Q"),
        _dur("2024-09-29", "2025-09-27", 112010000000, "2025-11-01", "FY", "10-K"),
        _dur("2025-09-28", "2025-12-27", 42097000000, "2026-01-29", "Q1", "10-Q"),
        _dur("2025-12-28", "2026-03-28", 29578000000, "2026-05-01", "Q2", "10-Q"),
    ]}},
    "Assets": {"label": "Assets", "units": {"USD": [
        _instant("2024-12-28", 344085000000, "2025-01-30"),
        _instant("2025-03-29", 331233000000, "2025-05-01"),
        _instant("2025-06-28", 331495000000, "2025-08-01"),
        _instant("2025-09-27", 359241000000, "2025-11-01"),
        _instant("2025-12-27", 379297000000, "2026-01-29"),
        _instant("2026-03-28", 371082000000, "2026-05-01"),
    ]}},
}}}


def _aapl_summary(end):
    from company_corpus.financials import attach_ttm_metrics, build_period_summaries
    summaries = build_period_summaries(AAPL_TTM_FACTS, company="Apple", company_current="Apple")
    attach_ttm_metrics(AAPL_TTM_FACTS, summaries)
    return next(s for s in summaries if s.period_end == end)


def test_attach_ttm_reproduces_bloomberg_aapl_roa():
    # Bloomberg-published quarterly ROA, reproduced to 4 dp by TTM NI / avg assets.
    dec = _aapl_summary(date(2025, 12, 27))
    assert dec.ttm["roa_ttm"]["value"] == pytest.approx(32.5629, abs=1e-3)
    mar = _aapl_summary(date(2026, 3, 28))
    assert mar.ttm["roa_ttm"]["value"] == pytest.approx(34.9060, abs=1e-3)


def test_attach_ttm_suppressed_when_year_ago_balance_missing():
    # The earliest quarter has no year-ago assets -> averaged metric omitted.
    early = _aapl_summary(date(2025, 3, 29))
    assert "roa_ttm" not in early.ttm


def test_normalized_rows_include_ttm_rows():
    from company_corpus.financials import normalized_rows
    dec = _aapl_summary(date(2025, 12, 27))
    rows = normalized_rows("0000320193", dec)
    roa = next(r for r in rows if r["concept"] == "roa_ttm")
    assert roa["kind"] == "derived_ttm"
    assert roa["value"] == pytest.approx(32.5629, abs=1e-3)


def test_flow_series_unions_fallback_tags():
    # Revenue split across vintages: 3-month + 9M YTD under the modern tag, but the
    # FISCAL-YEAR value under the older `Revenues` tag. The single-tag lookup would
    # miss the annual value, so the FY-end quarter (FY - 9M) could not be derived.
    facts = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"label": "Rev", "units": {"USD": [
            _dur("2024-09-29", "2024-12-28", 100, "2025-01-30", "Q1", "10-Q"),
            _dur("2024-12-29", "2025-03-29", 110, "2025-05-01", "Q2", "10-Q"),
            _dur("2025-03-30", "2025-06-28", 120, "2025-08-01", "Q3", "10-Q"),
            _dur("2024-09-29", "2025-06-28", 330, "2025-08-01", "Q3", "10-Q"),  # 9M YTD
        ]}},
        "Revenues": {"label": "Rev", "units": {"USD": [
            _dur("2024-09-29", "2025-09-27", 450, "2025-11-01", "FY", "10-K"),   # FY only on the old tag
        ]}},
    }}}
    s = _build_flow_series(flatten_points(facts), "USD")
    # The FY value is picked up from the fallback `Revenues` tag despite the
    # higher-priority tag carrying the quarters.
    assert s.annual["revenue"][date(2025, 9, 27)] == 450
    # The FY-end quarter is now derivable: 450 (FY) - 330 (9M YTD) = 120.
    assert _standalone_quarter(s, "revenue", date(2025, 9, 27)) == 120
