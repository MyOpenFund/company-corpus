"""IFRS filers on trimmed real data. Expected values were read in the filings (Review page).

NatWest Group (bank, GBP, 20-F + semiannual 6-K), AstraZeneca (USD, 20-F + semiannual 6-K) and
JBS N.V. (USD, 20-F + quarterly 6-K). Amounts are millions as printed (JBS prints thousands).
"""
from decimal import Decimal

import pytest

from company_corpus.concepts.dictionary import load_dictionary
from company_corpus.concepts.summaries import select_for_f1
from tests.concepts.support import fixture, millions, pick, selected


def _check(rows, day, frequency, expected):
    """``expected``: concept -> millions as printed, or None where the filing has no such line."""
    for concept, value in expected.items():
        freq = None if concept in _INSTANT else frequency
        r = pick(rows, concept, day, freq)
        if value is None:
            assert r is None, (concept, r)
        else:
            assert r is not None, concept
            assert millions(r) == Decimal(value), (concept, r["value"], r["original_tag"])


_INSTANT = {"assets", "liabilities", "equity", "equity_total", "cash", "long_term_debt",
            "lt_debt_current", "short_term_debt", "debt_current_total", "debt_total",
            "shares_outstanding"}


def test_natwest_a_bank_without_revenue_line():
    rows = selected("natwest")
    # 2025 20-F: income statement, balance sheet, cash flow statement (GBP m).
    _check(rows, "2025-12-31", "annual", {
        "revenue": None,                     # "Total income" is an extension tag; the ifrs-full
                                             # Revenue fact (30,243, gross) is in a note only
        "operating_income": None,            # "Operating profit before tax" is pretax income
        "pretax_income": "7708",             # Operating profit before tax
        "net_income": "5479",                # Attributable to: Ordinary shareholders
        "net_income_consolidated": "5834",   # Profit for the year
        "interest_expense": "12869",         # Interest payable
        "interest_net": "12829",             # Net interest income
        "assets": "714553",                  # Total assets
        "liabilities": "671940",             # Total liabilities
        "equity": "42599",                   # Owners' equity
        "equity_total": "42613",             # Total equity
        "cash": "95433",                     # Cash and cash equivalents at 31 December (CF)
        "long_term_debt": None,              # a bank: no borrowings line
        "debt_total": None,
        "cfo": "7072",                       # Net cash flows from operating activities
        "capex": "665",                      # Purchase of property, plant and equipment
        "dividends_paid": "2376",            # Dividends paid
        "buybacks": "579",                   # Shares repurchased
        "shares_outstanding": None,          # not tagged in the financial statements
    })
    eps = pick(rows, "eps_diluted", "2025-12-31", "annual")
    assert eps["value"] == Decimal("0.674") and eps["unit"] == "GBP/shares"   # 67.4p
    assets = pick(rows, "assets", "2025-12-31")
    assert assets["unit"] == "GBP" and assets["location"] == "statement" and assets["on_statement"]
    ni = pick(rows, "net_income", "2025-12-31", "annual")
    assert ni["original_tag"] == "ifrs-full:ProfitLossAttributableToOrdinaryEquityHoldersOfParentEntity"
    assert ni["review_status"] == "reviewed"
    assert millions(pick(rows, "wavg_shares_diluted", "2025-12-31", "annual")) == 8126   # EPS note
    assert millions(pick(rows, "goodwill", "2025-12-31")) == 5520                       # note 16
    # 2025 interim 6-K (H1, condensed statements).
    _check(rows, "2025-06-30", "semiannual", {
        "revenue": None,
        "operating_income": None,
        "net_income": "2488",                # Ordinary shareholders
        "assets": "730778",
        "liabilities": "688802",
        "equity": "41958",
        "cash": "102875",                    # Cash and cash equivalents at end of period
        "long_term_debt": None,
        "cfo": "5117",
        "capex": None,                       # the condensed cash flow statement has no such line
        "shares_outstanding": None,
    })
    assert pick(rows, "eps_diluted", "2025-06-30", "semiannual")["value"] == Decimal("0.305")


def test_astrazeneca_by_function_with_borrowings():
    rows = selected("astrazeneca")
    # 2025 20-F (USD m).
    _check(rows, "2025-12-31", "annual", {
        "revenue": "58739",                  # Total Revenue
        "cost_of_revenue": "10633",          # Cost of sales
        "gross_profit": "48106",
        "sga_expense": "19933",              # Selling, general and administrative expense
        "rnd_expense": "14232",
        "operating_income": "13743",         # Operating profit
        "pretax_income": "12402",
        "income_tax": "2169",                # Taxation
        "net_income": "10225",               # Profit attributable to owners of the Parent
        "net_income_nci": "8",
        "assets": "114074",
        "liabilities": "65355",
        "equity": "48667",                   # Total equity attributable to owners of parent
        "cash": "5711",                      # balance sheet line
        "long_term_debt": "24715",           # non-current Interest-bearing loans and borrowings
        "debt_current_total": "3104",        # current Interest-bearing loans and borrowings
        "debt_total": None,                  # note total 29,622 includes lease liabilities
        "cfo": "14575",
        "capex": "2810",
        "dividends_paid": "4971",
        "dep_amort": None,                   # the cash flow line includes impairment
        "shares_outstanding": None,
    })
    assert pick(rows, "eps_diluted", "2025-12-31", "annual")["value"] == Decimal("6.54")
    assert millions(pick(rows, "receivables", "2025-12-31")) == 10237     # note: trade receivables
    assert millions(pick(rows, "payables", "2025-12-31")) == 3820         # note: trade payables
    assert millions(pick(rows, "wavg_shares_diluted", "2025-12-31", "annual")) == 1562
    # The prior year, from the same 20-F.
    _check(rows, "2024-12-31", "annual", {"revenue": "54073", "operating_income": "10003",
                                          "net_income": "7035", "cfo": "11861"})
    # 2025 interim 6-K: H1 and Q2.
    _check(rows, "2025-06-30", "semiannual", {
        "revenue": "28045",
        "operating_income": "7182",
        "net_income": "5366",
        "assets": "112422",
        "liabilities": "67612",
        "equity": "44719",                   # Capital and reserves attributable to equity holders
        "cash": "7058",
        "long_term_debt": "24714",
        "debt_current_total": "6492",
        "lt_debt_current": "4461",           # net debt note: Current instalments of loans
        "cfo": "7099",
        "capex": "1088",
        "shares_outstanding": None,
    })
    assert pick(rows, "eps_diluted", "2025-06-30", "semiannual")["value"] == Decimal("3.44")
    assert millions(pick(rows, "revenue", "2025-06-30", "quarterly")) == 14457


def test_jbs_quarterly_6k_and_no_total_liabilities_line():
    rows = selected("jbs")
    # 2025 20-F (USD thousands, here in millions).
    _check(rows, "2025-12-31", "annual", {
        "revenue": "86184.182",              # NET REVENUE
        "gross_profit": "11299.280",
        "sga_expense": None,                 # Selling and G&A are two lines; no SG&A total
        "operating_income": "4159.677",      # OPERATING PROFIT
        "net_income": "2024.333",            # Company shareholders
        "net_income_consolidated": "2229.792",
        "interest_expense": "1677.957",      # note 22: Interest expense
        "interest_net": None,                # its InterestRevenueExpense fact is an antitrust item
        "assets": "45156.486",
        "liabilities": None,                 # no total liabilities line on the balance sheet
        "equity": "8703.580",                # Attributable to company shareholders
        "cash": "4565.136",
        "long_term_debt": "20257.483",       # non-current Loans and financing
        "short_term_debt": "833.085",        # current Loans and financing (not_reviewed)
        "debt_total": None,                  # its Borrowings fact is a related party payable
        "cfo": "2931.953",
        "capex": "2099.269",
        "dep_amort": "2308.520",
        "dividends_paid": "1573.855",
        "shares_outstanding": None,          # its NumberOfSharesOutstanding counts treasury shares
    })
    assert pick(rows, "eps_diluted", "2025-12-31", "annual")["value"] == Decimal("1.89")
    assert pick(rows, "short_term_debt", "2025-12-31")["review_status"] == "not_reviewed"
    # 2025 Q3 6-K: quarter and nine months.
    _check(rows, "2025-09-30", "quarterly", {
        "revenue": "22596.984",
        "operating_income": "1219.038",
        "net_income": "580.887",
        "assets": "44186.111",
        "liabilities": None,
        "equity": "8717.331",
        "cash": "3558.215",
        "long_term_debt": "19769.235",
        "shares_outstanding": None,
    })
    assert pick(rows, "eps_diluted", "2025-09-30", "quarterly")["value"] == Decimal("0.52")
    _check(rows, "2025-09-30", "ytd9", {"revenue": "63121.151", "cfo": "996.362",
                                       "capex": "1261.012"})


@pytest.mark.parametrize("name", ["marriott", "jpmorgan", "otis", "pepsico", "equinix"])
def test_the_ifrs_table_changes_no_us_selection(name):
    fx = fixture(name)
    both = select_for_f1(fx["facts"], fx["statements"], load_dictionary(("us_gaap", "ifrs_full")))
    alone = select_for_f1(fx["facts"], fx["statements"], load_dictionary(("us_gaap",)))
    def key(r):
        return r["concept"], r["valid_date"], r["frequency"], r["basis"]

    assert sorted(both.rows, key=key) == sorted(alone.rows, key=key)
