from datetime import date

import pytest

from company_corpus.concepts.coverage import coverage, format_coverage
from company_corpus.financials import (FlowSeries, _standalone_quarter, attach_ttm_metrics, build_period_summaries,
                                       compute_derived)
from tests.concepts.support import fixture, selected


def _v(**kw):
    return {k: {"value": v, "unit": "USD", "label": k} for k, v in kw.items()}


@pytest.mark.parametrize("values, expected", [
    (dict(long_term_debt=100, lt_debt_current=10, short_term_debt=5), 115),
    (dict(long_term_debt=100, debt_current_total=30), 130),
    (dict(long_term_debt=100, debt_current_total=30, short_term_debt=5), 130),
    (dict(long_term_debt=100, lt_debt_current=10, short_term_debt=25, debt_current_total=30), 130),  # no double count
    (dict(long_term_debt=100, debt_current_total=3, short_term_debt=5), 105),
    (dict(long_term_debt=100, debt_total=999), 999),
    (dict(long_term_debt=100, lt_debt_current=10, debt_total=50), 110),
    (dict(long_term_debt=100, long_term_debt_lines=300, lt_debt_current=10), 310),
    (dict(long_term_debt=100, long_term_debt_lines=30, lt_debt_current=10), 110),
    (dict(long_term_debt_lines=30, lt_debt_current=10), 40),
    (dict(long_term_debt_incl_current=100, short_term_debt=5), 105),
    (dict(long_term_debt_incl_current=100, lt_debt_current=10), 100),
    (dict(long_term_debt_incl_current=100, lt_debt_current=10, debt_current_total=30), 120),
    (dict(long_term_debt_incl_current=100, debt_current_total=30), 100),    # its current part is unknown
    (dict(long_term_debt_incl_current=0, debt_total=75859), 75859),         # a stray zero loses
    (dict(long_term_debt=0), 0),
    (dict(long_term_debt=100, lt_debt_current=10, long_term_debt_incl_current=90, short_term_debt=5), 115),
    (dict(long_term_debt=947, short_term_debt=460, long_term_debt_incl_current=1410), 1410),  # the same debt, once
    (dict(long_term_debt=100, short_term_debt=20, long_term_debt_incl_current=150), 170),     # another figure: adds
    (dict(long_term_debt=900, lt_debt_current=47, short_term_debt=460, long_term_debt_incl_current=1410), 1410),
    (dict(long_term_debt=947, debt_current_total=460, long_term_debt_incl_current=1410), 1410),
    (dict(long_term_debt=947, short_term_debt=460, long_term_debt_incl_current=1435), 1435),  # at 2% still one figure
    (dict(long_term_debt=947, short_term_debt=460, long_term_debt_incl_current=1440), 1900),  # beyond it, two
    # The accepted cost: a gross total within 2% hides a real short-term line (the true total is 1085).
    (dict(long_term_debt=1000, lt_debt_current=50, short_term_debt=15, long_term_debt_incl_current=1070), 1070),
    (dict(debt_total=500, lt_debt_current=10), 500),
    (dict(debt_components=70, lt_debt_current=10), 70),         # the lines include current maturities
    (dict(debt_components=70, short_term_debt=5), 75),
    (dict(debt_components=70, long_term_debt=50), 50),
    (dict(debt_components=70, long_term_debt=0), 70),           # a stray zero is no noncurrent figure
    (dict(debt_total=500, debt_components=70), 500),
    (dict(short_term_debt=5), 5),
    (dict(lt_debt_current=10, short_term_debt=5), 15),
    (dict(debt_current_total=30), 30),
    (dict(debt_current_total=30, short_term_debt=5), 30),
])
def test_total_debt_order(values, expected):
    assert compute_derived(_v(**values), debt_rule="concepts")["total_debt"]["value"] == expected


def _lines(on_statement, **kw):
    return {k: {"value": v, "unit": "USD", "label": k, "on_statement": k in on_statement}
            for k, v in kw.items()}


@pytest.mark.parametrize("on_statement, values, expected", [
    # NXP: the balance sheet's total, the note's short-term figure left out
    ({"long_term_debt", "debt_current_total"},
     dict(long_term_debt=10972, debt_current_total=1250, short_term_debt=2000, lt_debt_current=1250), 12222),
    # Parker-Hannifin: one current line on the balance sheet, a note's parts repeat it
    ({"long_term_debt", "lt_debt_current"},
     dict(long_term_debt=6766, lt_debt_current=1754, short_term_debt=1000, debt_current_total=706), 8520),
    # Walmart: two current lines on the balance sheet add up, a narrower total does not win
    ({"long_term_debt", "lt_debt_current", "short_term_debt"},
     dict(long_term_debt=34624, lt_debt_current=3542, short_term_debt=6596, debt_total=38166), 44762),
    # PepsiCo: a whole with no known current part adds nothing to a total with current maturities
    ({"long_term_debt", "debt_current_total", "short_term_debt"},
     dict(long_term_debt=42321, debt_current_total=6861, short_term_debt=6861, long_term_debt_incl_current=46351), 49182),
    # Johnson & Johnson: the one balance sheet line holds the maturities a note gives
    ({"long_term_debt", "short_term_debt"},
     dict(long_term_debt=39438, short_term_debt=8495, lt_debt_current=2000, debt_current_total=8500,
          long_term_debt_incl_current=41438), 47933),
    # AMD: one line under a tag that changed between filings
    ({"long_term_debt", "lt_debt_current", "short_term_debt"},
     dict(long_term_debt=2348, lt_debt_current=874, short_term_debt=874), 3222),
    # A bank: both lines on the statement
    ({"long_term_debt_incl_current", "short_term_debt"},
     dict(long_term_debt_incl_current=435206, short_term_debt=64776), 499982),
])
def test_only_the_balance_sheets_current_lines_count(on_statement, values, expected):
    assert compute_derived(_lines(on_statement, **values), debt_rule="concepts")["total_debt"]["value"] == expected


@pytest.mark.parametrize("ytd", [
    {date(2016, 6, 24): 7156, date(2016, 6, 30): 14583},
    {date(2016, 6, 30): 14583, date(2016, 6, 24): 7156},
])
def test_the_fourth_quarter_takes_the_latest_nine_months_in_any_row_order(ytd):
    # Johnson Controls FY2016: Tyco's nine months to June 24 and the merged company's to June 30.
    series = FlowSeries(quarterly={}, annual={"revenue": {date(2016, 9, 30): 20837}}, ytd9={"revenue": ytd})
    assert _standalone_quarter(series, "revenue", date(2016, 9, 30)) == 20837 - 14583


def test_a_debt_free_filer_has_no_total_debt():
    assert "total_debt" not in compute_derived(_v(assets=10), debt_rule="concepts")


def test_the_default_rule_still_reads_tags_for_the_other_pillars():
    values = {"long_term_debt": {"value": 1000, "unit": "EUR", "label": "x", "tag": "Borrowings"},
              "short_term_debt": {"value": 300, "unit": "EUR", "label": "x", "tag": "CurrentBorrowings"}}
    assert compute_derived(values)["total_debt"]["value"] == 1000


def test_net_debt_uses_bundled_cash_when_cash_is_empty():
    d = compute_derived(_v(long_term_debt=100, cash_and_st_investments=30, short_term_investments=7),
                        debt_rule="concepts")
    assert d["net_debt"]["value"] == 70


def test_one_filing_one_concept_and_a_tag_only_as_comparative():
    def rev(val, start, end):
        return {"val": val, "start": start, "end": end, "accn": "A", "form": "10-K", "filed": "2025-02-01"}

    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        rev(10, "2024-01-01", "2024-12-31"), rev(9, "2023-01-01", "2023-12-31")]}}}}}
    summaries = build_period_summaries(facts, company="X", company_current="X")
    attach_ttm_metrics(facts, summaries)
    assert [(s.fy, s.values["revenue"]["value"], s.values["revenue"]["location"]) for s in summaries] == [
        (2024, 10, "unknown"), (2023, 9, "unknown")]
    assert not summaries[0].derived.get("total_debt")


def test_marriott_total_debt_end_to_end():
    fx = fixture("marriott")
    (fy,) = [s for s in build_period_summaries(fx["facts"], company="M", company_current="M",
                                               statements=fx["statements"])
             if s.frequency == "annual" and s.period_end.isoformat() == "2025-12-31"]
    assert fy.values["long_term_debt"]["tag"] == "LongTermDebtAndCapitalLeaseObligations"
    assert fy.derived["total_debt"]["value"] == 16204000000


@pytest.mark.parametrize("name, end, total, cash, net_debt", [
    ("block", "2025-12-31", 7289018000, 6564092000, 724926000),      # note total beats a partial line
    ("eqt", "2025-12-31", 7800328000, None, None),                   # a partial line is not the total
    ("truist", "2025-12-31", 69802000000, None, None),               # short-term borrowings kept
    ("jpmorgan", "2025-12-31", 499982000000, None, None),            # total + short-term borrowings
    ("target", "2026-01-31", 16456000000, None, 10968000000),        # note Cash ignored, bundled used
    ("waters", "2025-12-31", 1410000000, None, None),                # LongTermDebt already holds short-term
    ("tmobile", "2025-12-31", 86282000000, None, None),              # same
])
def test_total_debt_on_real_filings(name, end, total, cash, net_debt):
    fx = fixture(name)
    (fy,) = [s for s in build_period_summaries(fx["facts"], company=name, company_current=name,
                                               statements=fx["statements"])
             if s.frequency == "annual" and s.period_end.isoformat() == end]
    assert fy.derived["total_debt"]["value"] == total
    if cash is not None:
        assert fy.values["cash"]["value"] == cash
    if name == "target":
        assert "cash" not in fy.values
    if net_debt is not None:
        assert fy.derived["net_debt"]["value"] == net_debt


def test_net_income_falls_back_to_the_common_line_on_the_income_statement():
    fx = fixture("caterpillar")
    (fy,) = [s for s in build_period_summaries(fx["facts"], company="C", company_current="C",
                                               statements=fx["statements"])
             if s.frequency == "annual" and s.period_end.isoformat() == "2025-12-31"]
    assert fy.values["net_income"]["tag"] == "NetIncomeLossAvailableToCommonStockholdersBasic"
    assert fy.values["net_income"]["value"] == 8884000000


def test_xbrl_reports_a_failed_statements_refresh_and_still_builds(config, xbrl_fetcher, monkeypatch):
    import company_corpus.pipeline as pipeline
    from company_corpus.opendata.statements import StatementsReport

    monkeypatch.setattr(pipeline, "refresh_statements", lambda *a, **kw: StatementsReport(
        write=True, errors=["2026q3: expected and missing (404)"]))
    rep = pipeline.fetch_financials(["320193"], config=config, fetcher=xbrl_fetcher, dry_run=False)
    assert rep.periods > 0 and rep.errors[0]["source"] == "sec_statements"


def _on_statement(name):
    return {(s["accn"], s["tag"]) for s in fixture(name)["statements"]}


def test_a_chain_tag_on_the_statement_without_a_value_is_counted():
    c = coverage(selected("hershey"), _on_statement("hershey"))
    assert c["coverage:eps_basic:absent"] == 1 and c["coverage:eps_basic:on_statement_no_value"] == 1
    assert c["coverage:assets:statement"] == 1
    assert c["coverage:rnd_expense:on_statement_no_value"] == 0
    ford = coverage(selected("ford"), _on_statement("ford"))
    assert ford["coverage:long_term_debt:on_statement_no_value"] == 1


def test_no_annual_period_is_counted_not_a_crash():
    assert coverage([], set()) == {"coverage:no_annual_period": 1}


def test_format_prints_one_line_per_concept_in_dictionary_order():
    lines = list(format_coverage(coverage(selected("marriott"), _on_statement("marriott"))))
    assert lines[0].startswith("coverage revenue: ") and any(" derived=1" in x for x in lines)


def test_format_prints_statements_missing_when_present():
    from collections import Counter

    lines = list(format_coverage(Counter({"statements_missing": 3})))
    assert any("statements_missing" in x and "3" in x for x in lines)
